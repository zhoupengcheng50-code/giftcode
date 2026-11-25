# -*- coding: utf-8 -*-
import os
import time
import json
import re
from datetime import datetime
from pathlib import Path
from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout, sync_playwright

from export_merger import write_xls, parse_single_export_file, validate_records
from credentials import load as load_credentials, save as save_credentials
from unified_parser import TIERS, reward_name


HISTORICAL_NAME = re.compile(r"越南[0-9]{4}网页支付累充\s*([0-9]+)礼包码([0-9]{8})")
WORKBENCH_NAME = re.compile(r"越南[0-9]{4}网页支付累充礼包码([0-9]{8})-([0-9]+)档")


def is_batch_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def batch_suffix(name):
    match = re.search(r"(?<![0-9])([0-9]{8})$", name)
    return match[1] if match and is_batch_date(match[1]) else None


def click_btn_by_text(target, text):
    target.locator("button:visible, .caption:visible").filter(
        has_text=re.compile(f"^{re.escape(text)}$")
    ).first.click(timeout=10000)


GMS_LOOKUP = r"""
    const tryGet = fn => { try { return fn(); } catch (_) { return null; } };
    const widget = window.dorado?.widget;
    const views = [typeof view === 'undefined' ? null : view, widget?.View?.TOP_VIEW].filter(Boolean);
    function components(ids) {
        return views.flatMap(owner => {
            const registry = owner._identifiedViewElements;
            if (!registry) return [];
            return ids.filter(id => Object.hasOwn(registry, id)).map(id => registry[id]).filter(Boolean);
        });
    }
    function isElementVisible(el) {
        if (!el) return false;
        if (el.getClientRects().length > 0) return true;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return el.offsetWidth > 0 || el.offsetHeight > 0 || (el.parentElement && isElementVisible(el.parentElement));
    }
    function gridHeaders(grid) {
        return Array.from(grid.querySelectorAll('thead, .header-table, .header-cell, .d-grid-header'))
            .map(node => node.innerText.trim()).join(' ');
    }
    function visibleGrids() {
        return Array.from(document.querySelectorAll('.d-grid')).filter(isElementVisible);
    }
    function gridRows(grid) {
        return Array.from(grid.querySelectorAll('tr.row, tr.current-row, tr.selected-row')).filter(row =>
            isElementVisible(row) && row.closest('.d-grid') === grid
        );
    }
    function rewardGrids() {
        return visibleGrids().filter(grid => /物品名|道具名/.test(gridHeaders(grid)));
    }
    function rewardRow(change, quantity) {
        const grids = rewardGrids();
        if (grids.length !== 1) return null;
        const normalize = value => value.normalize('NFKC').replace(/\s+/g, '');
        const matches = gridRows(grids[0]).filter(row => {
            const [id, name, count] = Array.from(row.cells).map(cell => cell.innerText.trim());
            return id && name && count && Number(id) === change.id
                && normalize(name) === normalize(change.backend_name) && Number(count) === quantity;
        });
        if (matches.length === 1) {
            try { matches[0].scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); } catch (_) {}
            return matches[0];
        }
        return null;
    }
    function rewardEditButton() {
        const grids = rewardGrids();
        if (grids.length !== 1) return null;
        for (let panel = grids[0]; panel; panel = panel.parentElement) {
            if (panel.querySelectorAll('.d-grid').length > 1 && panel !== grids[0]) break;
            const buttons = Array.from(panel.querySelectorAll('button, .caption, [role="button"], .d-button'))
                .filter(node => isElementVisible(node) && node.innerText.trim() === '编辑')
                .map(node => node.closest('button, [role="button"], .d-toolbar-button, .d-button') || node);
            const unique = [...new Set(buttons)];
            if (unique.length) {
                const btn = unique.length === 1 ? unique[0] : null;
                if (btn) {
                    try { btn.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); } catch (_) {}
                }
                return btn;
            }
        }
        return null;
    }
    function boundDataSet(grid) {
        const dataSet = grid ? tryGet(() => widget.Component.get(grid)?._dataSet) : null;
        return typeof dataSet?.getData === 'function' ? dataSet : null;
    }
    function activityTable() {
        const candidates = visibleGrids().map(grid => ({
            grid,
            rows: gridRows(grid).flatMap(row => {
                const [id, name] = Array.from(row.cells).map(cell => cell.innerText.trim());
                return /^\d+$/.test(id || '') && name ? [{id, name, row}] : [];
            }),
            header: gridHeaders(grid),
        }));
        const identified = candidates.filter(table => table.rows.some(({name}) =>
            /^越南\d{4}网页支付累充\s*\d+礼包码\d{8}/.test(name)
        ));
        if (identified.length === 1) return identified[0];
        const titled = candidates.filter(({header}) =>
            /活动名称|活动名/.test(header)
            || (/编号/.test(header) && /名称/.test(header) && !/物品|角色|服务器/.test(header))
        );
        return titled.length === 1 ? titled[0] : null;
    }
    function dataSet(ids) {
        return components(ids).find(candidate => typeof candidate.getData === 'function');
    }
    function selectedRow(row) {
        return !!row?.matches('.current-row, .selected-row, .row-current, .current, .selected, [aria-selected="true"]');
    }
"""


def gms_script(body):
    return f"(arg) => {{ {GMS_LOOKUP}\n{body} }}"


def evaluate_gms(ctx, body, arg=None):
    return ctx.evaluate(gms_script(body), arg)


class GiftCodeAutomator:
    def __init__(self, job, work_dir, log, progress):
        self.job = job
        self.work_dir = str(work_dir)
        self.log = log
        self.set_progress = progress
        self.url = "https://xzvn.forceforever.com/xz-vn-gms/bdf2.core.view.frame.main.MainFrame1.d"
        self.username, self.password = job.get("credentials") or load_credentials()
        self.download_dir = job["directory"]
        self.progress_dir = job.get("progress_directory", self.download_dir)
        self.expire_date_str = job["expire_time"]
        self.code_count = job["count"]

    def wait_for_loading(self, ctx):
        ctx.locator(".d-loading:visible, .d-list-loading:visible").first.wait_for(
            state="hidden", timeout=15000
        )

    def login(self, page: Page, context):
        self.log(f"正在打开后台系统: {self.url}")
        self.set_progress(5, "系统初始化", "正在连接后台系统...")
        session_file = os.path.join(self.work_dir, "session_state.json")
        login_url = "https://xzvn.forceforever.com/xz-vn-gms/login.jsp"
        
        try:
            page.goto(self.url, timeout=45000)
            page.wait_for_load_state("networkidle")
        except Exception as e:
            self.log(f"页面初次访问提示: {str(e)}", "WARN")
        time.sleep(1.5)
        
        # 1. 检查是否已经处于正常登录的主界面
        if page.locator("text=角色管理").count() > 0 or (page.locator("text=BDF").count() > 0 and page.locator("#username_").count() == 0 and page.locator("text=权限不足").count() == 0):
            self.log("检测到有效会话，已处于登录状态！", "SUCCESS")
            return

        # 2. 检查是否被踢下线或提示权限不足（旧 cookie 失效）
        is_invalid = False
        if page.locator("text=被迫下线").count() > 0:
            self.log("检测到账号在另一地点登录导致被迫下线，重置登录态...", "WARN")
            is_invalid = True
        elif page.locator("text=权限不足").count() > 0:
            self.log("检测到权限不足/会话失效，重置登录态...", "WARN")
            is_invalid = True

        if is_invalid:
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                except Exception:
                    pass
            try:
                context.clear_cookies()
            except Exception:
                pass
            page.goto(login_url, timeout=30000)
            page.wait_for_load_state("networkidle")
            time.sleep(1)

        # 3. 如果未在主界面且无输入框，前往登录页
        if page.locator("#username_").count() == 0 and page.locator("text=角色管理").count() == 0:
            self.log("未检测到有效主页或登录表单，导航至登录页...", "INFO")
            page.goto(login_url, timeout=30000)
            page.wait_for_load_state("networkidle")
            time.sleep(1)

        # 4. 执行自动登录
        username_input = page.locator("#username_")
        if username_input.count() > 0 and username_input.is_visible():
            if not self.username or not self.password:
                self.log("提示: 未配置账号密码，请在弹出的浏览器中手动登录！", "WARN")
                self.set_progress(8, "等待用户登录", "请在弹出的浏览器窗口中输入账号密码登录...")
                page.wait_for_selector("#username_", state="hidden", timeout=300000)
            else:
                username_input.fill(self.username)
                page.locator("#password_").fill(self.password)
                time.sleep(0.5)
                page.locator("input[name='btn_Login'], input[type='submit']").click()
                time.sleep(2)
                page.wait_for_load_state("networkidle")
                
                error_div = page.locator("#errorInfo")
                if error_div.count() > 0 and error_div.inner_text().strip():
                    err_msg = error_div.inner_text().strip()
                    self.log(f"登录提示: {err_msg}", "ERROR")
                    raise RuntimeError("后台登录失败，请在工作台重新填写凭据")

        # 5. 登录后若由于 Spring Security 拦截重定向到权限不足页，重新前往主界面
        if page.locator("text=权限不足").count() > 0 or page.locator("text=角色管理").count() == 0:
            self.log("正在重定向进入后台主界面...", "INFO")
            page.goto(self.url, timeout=30000)
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)

        self.log("验证后台主界面加载...", "INFO")
        try:
            page.wait_for_selector("text=角色管理", timeout=20000)
        except Exception:
            page.locator("text=BDF").first.wait_for(timeout=10000)
            
        self.log("成功进入后台系统！", "SUCCESS")
        self.set_progress(10, "系统就绪", "成功进入后台系统")
        
        try:
            context.storage_state(path=session_file)
            self.log("登录态已自动缓存，下次可免密启动！", "INFO")
        except Exception:
            pass
        time.sleep(1)

    def open_gift_code_page(self, page: Page):
        self.log("正在寻找并打开 [游戏管理 -> 普通激活码]...")
        self.set_progress(12, "导航中", "打开普通激活码页面...")

        if any("NoviceCard" in frame.url for frame in page.frames):
            return self.wait_for_activity_frame(page)

        tab = page.locator(".caption:text-is('普通激活码'), span:text-is('普通激活码')")
        if tab.count() > 0 and tab.first.is_visible():
            self.log("点击工作区页签 [普通激活码]...")
            tab.first.click()
            time.sleep(1)

        gm = page.locator('.caption:text-is("游戏管理")')
        if gm.count() > 0:
            self.log("展开左侧菜单 [游戏管理]...")
            try:
                gm.first.click()
                time.sleep(0.8)
            except Exception:
                pass

        self.log("点击左侧节点 [普通激活码]...")
        node = page.locator('.caption:text-is("普通激活码"), .node-label:text-is("普通激活码")')
        if node.count() > 0:
            node.first.click()
            time.sleep(0.5)
            node.first.dblclick()
        else:
            page.get_by_text("普通激活码", exact=True).first.click()
            
        return self.wait_for_activity_frame(page)

    def wait_for_activity_frame(self, page):
        saw_frame = False
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            for f in page.frames:
                if "NoviceCard" not in f.url:
                    continue
                saw_frame = True
                try:
                    ready = evaluate_gms(f, """
                        return (activityTable()?.rows.length ?? 0) > 0;
                    """)
                except PlaywrightError:
                    if page.is_closed():
                        raise
                    continue
                if ready:
                    self.log("普通激活码活动列表已就绪", "SUCCESS")
                    return f
            page.wait_for_timeout(300)

        if saw_frame:
            raise RuntimeError("普通激活码页面已打开，但 25 秒内没有读到活动记录。请查看活动表是否为空或显示加载错误。")
        raise RuntimeError("点击普通激活码后，25 秒内没有找到该页面的工作区。")

    @staticmethod
    def activity_list(ctx):
        try:
            result = ctx.wait_for_function(gms_script("""
                const rows = activityTable()?.rows || [];
                return rows.length ? rows.map(({id, name}) => ({id, name})) : false;
            """), polling=300, timeout=20000)
        except PlaywrightTimeout as exc:
            raise RuntimeError(
                "已打开普通激活码页面，但 20 秒内未读到活动记录。请检查页面活动表是否有数据或显示加载错误。"
            ) from exc
        try:
            return result.json_value()
        finally:
            result.dispose()

    def clear_obsolete_activity_dialog(self, ctx):
        for root in (ctx, ctx.page):
            dialog = root.locator(".d-dialog:visible, .d-message-box:visible").filter(
                has_text=re.compile(r"未定义的属性[^\r\n]*activityDs")
            ).first
            if dialog.is_visible():
                dialog.get_by_text("确定", exact=True).click(timeout=3000)
                dialog.wait_for(state="hidden", timeout=4000)
                self.log("已关闭旧版组件查询留下的活动属性提示", "WARN")
        for root in (ctx, ctx.page):
            mask = root.locator(".d-modal-mask:visible, .i-modal-mask:visible").first
            try:
                mask.wait_for(state="hidden", timeout=4000)
            except PlaywrightTimeout as exc:
                raise RuntimeError("后台仍有弹窗遮挡活动列表，请查看可见浏览器中的提示。") from exc

    def select_activity(self, ctx, activity_id):
        self.click_activity_row(ctx, activity_id)
        try:
            self.wait_for_loading(ctx)
            return
        except PlaywrightTimeout:
            self.log(f"活动 {activity_id} 的详情加载超时，正在刷新普通激活码页面并重新选中同一活动。", "WARN")
        try:
            ctx.goto(ctx.url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightError as exc:
            raise RuntimeError(f"活动 {activity_id} 加载超时，重新打开普通激活码页面也失败了。") from exc
        self.activity_list(ctx)
        self.click_activity_row(ctx, activity_id)
        try:
            self.wait_for_loading(ctx)
        except PlaywrightTimeout as exc:
            raise RuntimeError(f"活动 {activity_id} 的详情刷新后仍在加载，本次已暂停，可沿用进度重试。") from exc

    def click_activity_row(self, ctx, activity_id):
        self.clear_obsolete_activity_dialog(ctx)
        handle = ctx.evaluate_handle(gms_script("""
            const candidates = activityTable()?.rows.filter(item => item.id === arg) || [];
            return candidates.length === 1 ? candidates[0].row : null;
        """), activity_id)
        try:
            row = handle.as_element()
            if not row:
                raise RuntimeError(f"活动表中找不到唯一的后台活动 ID {activity_id}")
            row.click(timeout=8000)
        except PlaywrightTimeout as exc:
            raise RuntimeError(f"活动 {activity_id} 无法点击，请查看后台活动表是否被遮挡。") from exc
        finally:
            handle.dispose()
        try:
            selected = ctx.wait_for_function(gms_script("""
                const table = activityTable();
                const target = table?.rows.find(item => item.id === arg);
                if (!target) return false;
                if (selectedRow(target.row)) return true;
                const ds = boundDataSet(table.grid);
                const data = tryGet(() => ds.getData());
                const current = data?.current || tryGet(() => data.getCurrent());
                return String(tryGet(() => current.get('id'))) === arg;
            """), arg=activity_id, polling=150, timeout=5000)
        except PlaywrightTimeout as exc:
            raise RuntimeError(f"已点击活动 {activity_id}，但后台未确认这条记录已选中。") from exc
        selected.dispose()

    @staticmethod
    def read_rewards(ctx):
        script = gms_script(r"""
            const grids = rewardGrids();
            if (grids.length !== 1) return false;
            const rows = gridRows(grids[0]);
            const items = rows.map(row => {
                const [id, name, qty] = Array.from(row.cells).map(cell => cell.innerText.trim());
                return /^\d+$/.test(id || '') && name && /^\d+$/.test(qty || '')
                    ? {id: Number(id), name, qty: Number(qty)} : null;
            });
            if (items.length) return items.every(Boolean) ? {items} : false;
            const data = tryGet(() => boundDataSet(grids[0]).getData());
            return (data?.entityCount ?? data?.size) === 0 ? {items: []} : false;
        """)
        try:
            handle = ctx.wait_for_function(script, polling=300, timeout=12000)
        except PlaywrightTimeout as exc:
            raise RuntimeError("已选中活动，但等待奖励表加载超时。请检查后端是否显示了该活动的奖励。") from exc
        try:
            return handle.json_value()["items"]
        finally:
            handle.dispose()

    @staticmethod
    def compare(expected, actual):
        by_name = {}
        for index, item in enumerate(actual):
            by_name.setdefault(reward_name(item["name"]), []).append((index, item))

        consumed = set()
        results = []
        for name, quantity in expected.items():
            candidates = [pair for pair in by_name.get(reward_name(name), []) if pair[0] not in consumed]
            exact = [pair for pair in candidates if pair[1]["qty"] == quantity]
            if exact:
                index, item = min(exact, key=lambda pair: (pair[1]["id"], pair[1]["name"]))
                consumed.add(index)
                results.append({"id": item["id"], "name": name, "backend_name": item["name"],
                                "expected": quantity, "actual": quantity, "status": "same", "matched": True})
            elif len(candidates) == 1:
                index, item = candidates[0]
                consumed.add(index)
                results.append({"id": item["id"], "name": name, "backend_name": item["name"],
                                "expected": quantity, "actual": item["qty"], "status": "quantity", "matched": False})
            else:
                review = "后台有多条同名奖励，无法确定要改哪条" if candidates else "后台找不到同名奖励"
                results.append({"id": None, "name": name, "expected": quantity, "actual": None,
                                "status": "review", "matched": False, "reason": review})

        kept = (item for index, item in enumerate(actual) if index not in consumed)
        for item in sorted(kept, key=lambda item: (item["id"], item["name"], item["qty"])):
            results.append({"id": item["id"], "name": item["name"], "expected": None,
                            "actual": item["qty"], "status": "kept", "matched": True})
        return results

    def previous_batch(self, activities):
        current = self.job["batch_date"]
        by_batch = {}

        def add(batch, tier, activity):
            if tier in TIERS and is_batch_date(batch) and batch < current:
                by_batch.setdefault(batch, {}).setdefault(tier, {})[activity["id"]] = activity

        for activity in activities:
            match = HISTORICAL_NAME.fullmatch(activity["name"])
            if match:
                tier, batch = match.groups()
                add(batch, tier, activity)
                continue
            match = WORKBENCH_NAME.fullmatch(activity["name"])
            if match:
                batch, tier = match.groups()
                add(batch, tier, activity)

        visible = {row["id"]: row for row in activities}
        for completed in self.download_dir.parent.glob("*/completed.json"):
            try:
                previous = json.loads(completed.read_text(encoding="utf-8"))
                group = previous["activities"]
                batch = previous.get("batch_date") or batch_suffix(previous["name"])
                for tier, row in group.items():
                    if visible.get(row["id"]) == row:
                        add(batch, tier, row)
            except (KeyError, TypeError, ValueError, OSError):
                continue

        saved = self.job.get("previous_plan")
        if saved:
            baseline = saved["baseline"]
            if not is_batch_date(baseline) or baseline >= current:
                raise RuntimeError("旧任务记录的来源日期不早于本期或无法识别，已暂停恢复任务")
            sources = {tier: value["source"] for tier, value in saved["tiers"].items()}
            if set(sources) != set(TIERS) or any(visible.get(row["id"]) != row for row in sources.values()):
                raise RuntimeError(f"恢复任务时无法确认 {baseline} 的原始八档活动")
            return baseline, sources

        if not by_batch:
            raise RuntimeError(f"当前后台活动列表没有找到早于 {current} 的网页支付累充批次")
        baseline = max(by_batch)
        group = by_batch[baseline]
        issues = [f"{tier} 档{'重复' if tier in group else '缺失'}" for tier in TIERS if len(group.get(tier, {})) != 1]
        if issues:
            raise RuntimeError(f"上一期 {baseline} 无法确认完整八档：{'、'.join(issues)}")
        return baseline, {tier: next(iter(group[tier].values())) for tier in TIERS}

    def preview(self, ctx):
        baseline, selected = self.previous_batch(self.activity_list(ctx))
        self.log(f"本期 {self.job['batch_date']}，上一期 {baseline}，已找到八个来源档位")
        preview = {}
        for index, (tier, expected) in enumerate(self.job["rewards"].items()):
            self.set_progress(10 + index * 10, tier, "读取往期奖励")
            source = selected[tier]
            self.select_activity(ctx, source["id"])
            diff = self.compare(expected, self.read_rewards(ctx))
            preview[tier] = {"source": source, "target": self.target_name(tier), "items": diff}
        return {"batch_date": self.job["batch_date"], "baseline": baseline, "tiers": preview}

    def target_name(self, tier):
        return f"{self.job['activity_name']}-{tier}档"

    def exact_confirmation(self, ctx, operation):
        for root in (ctx, ctx.page):
            candidate = root.locator(".d-message-box:visible, .d-dialog:visible").filter(
                has_text=re.compile(operation)
            ).filter(has=root.get_by_text("是", exact=True))
            if candidate.count() == 1:
                candidate.get_by_text("是", exact=True).click(timeout=6000)
                return

    def write_state(self, state):
        path = self.progress_dir / "progress.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def copy_activity(self, ctx, source, target, checkpoint, state):
        if "copy_candidates_before" not in checkpoint:
            before = [row["id"] for row in self.activity_list(ctx)]
            self.select_activity(ctx, source["id"])
            checkpoint["copy_candidates_before"] = before
            self.write_state(state)
            click_btn_by_text(ctx, "拷贝")
            ctx.wait_for_timeout(600)
            self.exact_confirmation(ctx, "拷贝|复制")

        old_ids = set(checkpoint["copy_candidates_before"])
        for _ in range(24):
            matches = [row for row in self.activity_list(ctx)
                       if row["id"] not in old_ids
                       and row["name"] in (source["name"] + "(copy)", source["name"] + " (copy)")]
            if len(matches) > 1:
                raise RuntimeError(f"发现多个候选副本，已停止处理 {target}")
            if matches:
                break
            ctx.wait_for_timeout(500)
        else:
            raise RuntimeError(f"无法确认 {target} 的副本，已暂停，避免重复拷贝")
        activity_id = matches[0]["id"]
        checkpoint["id"] = activity_id
        self.write_state(state)
        return activity_id

    def ensure_target(self, ctx, source, name, checkpoint, state):
        activities = self.activity_list(ctx)
        existing = [row for row in activities if row["name"] == name]
        activity_id = checkpoint.get("id")
        if existing and (len(existing) != 1 or existing[0]["id"] != activity_id):
            raise RuntimeError(f"后台已有同名活动「{name}」，请使用唯一的本次活动名称")
        if existing:
            return activity_id
        copy_names = (source["name"] + "(copy)", source["name"] + " (copy)")
        if activity_id is not None and not any(row["id"] == activity_id for row in activities):
            if checkpoint.get("submitted"):
                raise RuntimeError(f"已提交发码的活动 {activity_id} 不在当前列表，已暂停重建")
            if not any(row["id"] == source["id"] and row["name"] == source["name"] for row in activities):
                raise RuntimeError(f"未找到「{name}」对应的上一期来源活动，已暂停重建")
            before = set(checkpoint.get("copy_candidates_before", ()))
            if any(row["name"] in copy_names and row["id"] not in before for row in activities):
                raise RuntimeError(f"发现「{name}」可能已有未命名副本，已暂停重建")
            self.log(f"未发码的活动 {activity_id} 已不在列表，从上一期重新复制「{name}」")
            checkpoint.clear()
            activity_id = None
        if activity_id is None:
            activity_id = self.copy_activity(ctx, source, name, checkpoint, state)
        candidate = next((row for row in self.activity_list(ctx) if row["id"] == activity_id), None)
        if not candidate or candidate["name"] not in copy_names:
            raise RuntimeError(f"无法确认活动 {activity_id} 的来源")
        self.select_activity(ctx, activity_id)
        click_btn_by_text(ctx, "编辑")
        dialog = ctx.locator("#d_dialogActivity:visible, .d-dialog:visible:has-text('活动名称')").first
        dialog.wait_for(state="visible", timeout=10000)
        name_field = dialog.locator("tr").filter(has_text="名称").locator("input:visible:not([readonly])")
        if name_field.count() != 1:
            name_field = dialog.locator("input:visible:not([readonly])")
        if name_field.count() != 1:
            raise RuntimeError("无法唯一定位活动名称输入框")
        name_field.fill(name)
        name_field.press("Tab")
        dialog.get_by_text("保存", exact=True).click(timeout=10000)
        ctx.wait_for_timeout(600)
        self.exact_confirmation(ctx, "保存")
        dialog.wait_for(state="hidden", timeout=15000)
        matches = [row for row in self.activity_list(ctx) if row["id"] == activity_id and row["name"] == name]
        if len(matches) != 1:
            raise RuntimeError(f"保存后无法确认活动名称「{name}」")
        return activity_id

    def wait_reward_editor(self, ctx):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for root in (ctx, ctx.page):
                dialogs = root.locator(".d-dialog:visible, [role=dialog]:visible").filter(has_text="数量")
                for dialog in dialogs.all():
                    fields = [field for field in dialog.locator("input:visible").all() if field.is_editable()]
                    labeled = [
                        field for field in fields if field.evaluate("""input => {
                            if (/数量/.test(input.getAttribute('aria-label') || '')) return true;
                            if (Array.from(input.labels || []).some(label => /数量/.test(label.innerText))) return true;
                            for (let row = input.parentElement; row && !row.matches('.d-dialog, [role=dialog]'); row = row.parentElement) {
                                if (Array.from(row.querySelectorAll('input')).filter(el => el.getClientRects().length && !el.readOnly && !el.disabled).length > 1) break;
                                if (/数量/.test(row.innerText)) return true;
                            }
                            return false;
                        }""")
                    ]
                    if len(labeled) == 1:
                        return dialog, labeled[0]
                    if not labeled and len(fields) == 1:
                        return dialog, fields[0]
            ctx.wait_for_timeout(200)
        raise RuntimeError("打开物品奖励编辑后，未找到唯一的数量输入框。")

    def edit_reward_quantity(self, ctx, change):
        name = change["name"]
        row_handle = ctx.evaluate_handle(
            gms_script("return rewardRow(arg, arg.actual);"), change
        )
        try:
            row = row_handle.as_element()
            if not row:
                raise RuntimeError(f"无法唯一定位「{name}」的原有奖励记录，或原数量已变化。")
            row.click(timeout=8000)
        finally:
            row_handle.dispose()

        selection = ctx.wait_for_function(
            gms_script("return selectedRow(rewardRow(arg, arg.actual));"),
            arg=change, polling=150, timeout=5000,
        )
        selection.dispose()
        edit_handle = ctx.evaluate_handle(gms_script("return rewardEditButton();"))
        try:
            edit = edit_handle.as_element()
            if not edit:
                raise RuntimeError("未找到物品奖励区唯一的「编辑」按钮。")
            edit.click(timeout=8000)
        finally:
            edit_handle.dispose()

        dialog, field = self.wait_reward_editor(ctx)
        if field.input_value().strip() != str(change["actual"]):
            raise RuntimeError(f"「{name}」编辑框的原数量与列表不一致，已暂停。")
        field.fill(str(change["expected"]), timeout=5000)
        field.press("Tab")
        if field.input_value().strip() != str(change["expected"]):
            raise RuntimeError(f"「{name}」的数量输入未生效，已暂停保存。")

        save = dialog.get_by_text("保存", exact=True)
        if save.count() == 0:
            save = dialog.get_by_text("确定", exact=True)
        if save.count() != 1:
            raise RuntimeError("奖励编辑窗口未找到唯一的保存按钮。")
        save.click(timeout=8000)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.exact_confirmation(ctx, "保存")
            for root in (ctx, ctx.page):
                notice = root.locator(".d-message-box:visible, .d-dialog:visible").filter(
                    has_text=re.compile("保存成功|操作成功")
                ).first
                if notice.is_visible():
                    okay = notice.get_by_text("确定", exact=True)
                    if okay.count() == 1:
                        okay.click(timeout=3000)
            if not dialog.is_visible() and evaluate_gms(ctx, "return !!rewardRow(arg, arg.expected);", change):
                self.log(f"已核对「{name}」数量 {change['actual']} → {change['expected']}")
                return
            ctx.wait_for_timeout(200)
        raise RuntimeError(f"「{name}」保存后核验超时，请查看浏览器中的后台提示。")

    def apply_rewards(self, ctx, activity_id, expected):
        self.select_activity(ctx, activity_id)
        comparison = self.compare(expected, self.read_rewards(ctx))
        pending = [item["name"] for item in comparison if item["status"] == "review"]
        if pending:
            raise RuntimeError(f"无法在现有奖励中定位：{'、'.join(pending)}")
        changes = [item for item in comparison if item["status"] == "quantity"]
        for change in changes:
            try:
                self.edit_reward_quantity(ctx, change)
            except PlaywrightTimeout as exc:
                raise RuntimeError(f"「{change['name']}」编辑或保存后核验超时，请查看浏览器中的后台提示。") from exc
        return bool(changes)

    @staticmethod
    def activation_list(ctx):
        return evaluate_gms(ctx, r"""
            const cards = Array.from(document.querySelectorAll('.d-grid')).filter(grid => {
                const header = gridHeaders(grid);
                return /激活码/.test(header) && /创建时间|批次号|失效时间/.test(header);
            });
            if (cards.length !== 1) return null;
            const grid = cards[0];
            let panel = grid;
            while (panel.parentElement && panel.tagName !== 'BODY') {
                if (/导出\s*Excel/.test(panel.innerText) && /生成/.test(panel.innerText)) break;
                panel = panel.parentElement;
            }
            if (panel.querySelector('.d-loading:not([style*="display: none"]), .d-list-loading:not([style*="display: none"])')
                && [...panel.querySelectorAll('.d-loading, .d-list-loading')].some(node => node.getClientRects().length)) return null;
            const totals = [...(panel.innerText || '').matchAll(/([0-9][0-9,]*)\s*(?:笔|条)\s*记录/g)];
            const total = totals.length >= 1 ? Number(totals[0][1].replace(/,/g, '')) : null;
            const populated = gridRows(grid).some(row =>
                row.cells.length > 2 && row.cells.item(0).innerText.trim() && row.cells.item(1).innerText.trim()
            );
            const hasGenerateBtn = Array.from(document.querySelectorAll('.d-button, button, a, span'))
                .some(n => /^(?:生成|导出\s*Excel)$/.test(n.innerText.trim()));
            const toolbar = (/导出\s*Excel/.test(panel.innerText) && /生成/.test(panel.innerText)) || hasGenerateBtn;
            const empty = total === 0 || populated === false || /暂无数据|没有数据|暂无激活码|无记录/.test(grid.innerText);
            return {total, populated, ready: toolbar || empty, empty};
        """)

    def wait_for_empty_activation_list(self, ctx):
        deadline = time.monotonic() + 18
        stable_since = None
        while time.monotonic() < deadline:
            listing = self.activation_list(ctx)
            if listing and (listing["populated"] or (listing["total"] or 0) > 0):
                raise RuntimeError("当前活动已有激活码，已暂停再次生成。")
            if listing and listing["ready"]:
                if listing["empty"]:
                    return
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 0.8:
                    return
            else:
                stable_since = None
            ctx.wait_for_timeout(200)
        raise RuntimeError("当前活动的激活卡列表尚未就绪，无法确认是否已有激活码。")

    def fill_generation_fields(self, dialog):
        fields = dialog.evaluate_handle(r"""dialog => {
            const roles = {
                count: /^(?:数量|生成数量|生成个数|(?:激活码|激活卡)(?:生成)?数量)$/,
                registration: /^(?:角色)?注册(?:限制)?天数(?:限制)?(?:\(天\))?$/,
                expiry: /^(?:激活码|激活卡)?(?:失效|过期)(?:时间|日期)?$|^(?:截止时间|有效期至)$/,
            };
            const identify = text => {
                const label = String(text || '').normalize('NFKC').replace(/[\s:*＊]/g, '');
                return Object.keys(roles).find(role => roles[role].test(label));
            };
            const isVisible = el => {
                if (!el) return false;
                if (el.getClientRects().length > 0) return true;
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && (el.offsetWidth > 0 || el.offsetHeight > 0);
            };
            const inputs = [...dialog.querySelectorAll('input:not([type=hidden])')].filter(isVisible);
            const calendars = [...dialog.querySelectorAll('.d-trigger-icon-date')].filter(isVisible);
            const matches = {count: [], registration: [], expiry: []};
            for (const input of inputs) {
                const explicit = [
                    input.getAttribute('aria-label'),
                    ...[...(input.labels || [])].map(label => label.textContent),
                    ...(input.getAttribute('aria-labelledby') || '').split(/\s+/)
                        .map(id => dialog.ownerDocument.getElementById(id)?.textContent),
                ].map(identify).filter(Boolean);
                const evidence = new Set(explicit);
                if (!evidence.size) {
                    for (let container = input.parentElement; container && container !== dialog;
                         container = container.parentElement) {
                        if (inputs.filter(field => container.contains(field)).length !== 1) break;
                        const labels = [...container.querySelectorAll('label, td, th, span, div')]
                            .filter(node => !node.contains(input)).map(node => identify(node.textContent));
                        labels.push(identify([...container.childNodes]
                            .filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('')));
                        const adjacent = container.previousElementSibling;
                        if (adjacent && !inputs.some(field => adjacent.contains(field))) {
                            labels.push(identify(adjacent.textContent));
                        }
                        for (const label of labels.filter(Boolean)) evidence.add(label);
                        if (evidence.size) break;
                    }
                }
                if (evidence.size === 1) matches[[...evidence][0]].push(input);
            }
            return {
                count: matches.count.length === 1 ? matches.count[0] : null,
                registration: matches.registration.length === 1 ? matches.registration[0] : null,
                expiry: calendars.length === 1 ? calendars[0]
                    : calendars.length === 0 && matches.expiry.length === 1 ? matches.expiry[0] : null,
            };
        }""")
        handles = []
        try:
            for key, label in (("count", "数量"), ("registration", "注册天数"), ("expiry", "失效时间")):
                handle = fields.get_property(key)
                handles.append(handle)
                if handle.as_element() is None:
                    raise RuntimeError(f"生成窗口未找到唯一的「{label}」字段，未填写任何参数。")
            count, registration, expiry = (handle.as_element() for handle in handles)
            if not fields.evaluate("x => new Set([x.count, x.registration, x.expiry]).size === 3"):
                raise RuntimeError("生成窗口的三个字段无法区分，未填写任何参数。")
            if registration.input_value().strip() != "0":
                raise RuntimeError("后台注册天数不是默认 0，已停止发码。")

            count.fill(str(self.code_count), timeout=5000)
            count.press("Tab")
            if expiry.evaluate("node => node.matches('.d-trigger-icon-date')"):
                self.select_generation_calendar(expiry)
            else:
                self.fill_generation_expiry(expiry)
            if count.input_value().strip() != str(self.code_count) or registration.input_value().strip() != "0":
                raise RuntimeError("生成前校验失败：数量与工作台不一致，或后台注册天数不是默认 0。")
        finally:
            for handle in handles:
                handle.dispose()
            fields.dispose()

    def select_generation_calendar(self, trigger):
        expected = datetime.fromisoformat(self.expire_date_str)
        frame = trigger.owner_frame()
        if frame is None:
            raise RuntimeError("失效时间日历已离开生成窗口。")
        button = trigger.evaluate_handle("icon => icon.closest('.i-icon-button, button, [role=button]') || icon")
        try:
            button.as_element().click(timeout=5000)
        finally:
            button.dispose()
        calendar = frame.locator(".d-date-picker:visible")
        calendar.wait_for(state="visible", timeout=6000)

        heading = calendar.locator(".year-month-label")
        target_month = expected.year * 12 + expected.month
        for _ in range(25):
            numbers = re.findall(r"[0-9]+", heading.inner_text(timeout=5000))
            years = [int(value) for value in numbers if len(value) == 4]
            months = [int(value) for value in numbers if len(value) <= 2 and 1 <= int(value) <= 12]
            if len(years) != 1 or len(months) != 1:
                raise RuntimeError("无法读取失效时间日历当前显示的年月。")
            current_month = years[0] * 12 + months[0]
            if current_month == target_month:
                break
            step = 1 if target_month > current_month else -1
            arrow = ".next-month-button" if step > 0 else ".pre-month-button"
            arrow_button = calendar.locator(f".i-icon-button:has({arrow})")
            (arrow_button if arrow_button.count() == 1 else calendar.locator(arrow)).click(timeout=5000)
            updated = frame.wait_for_function(r"""target => {
                const label = [...document.querySelectorAll('.d-date-picker .year-month-label')]
                    .find(element => element.getClientRects().length);
                const numbers = label?.innerText.match(/[0-9]+/g) || [];
                const year = numbers.find(part => part.length === 4);
                const month = numbers.find(part => part.length <= 2 && Number(part) >= 1 && Number(part) <= 12);
                return !!year && !!month && Number(year) * 12 + Number(month) === target;
            }""", arg=current_month + step, timeout=5000)
            updated.dispose()
        else:
            raise RuntimeError("未能将失效时间日历切换到目标月份。")

        day = calendar.evaluate_handle(r"""(picker, target) => {
            const block = picker.querySelector('.date-block');
            const table = block?.querySelector('.date-table');
            if (!table) return null;
            const controls = window.dorado?.widget?.Control;
            const region = controls?.findParentControl?.(picker, 'dorado.widget.DatePicker')?._visibleDateRegion;
            let cells;
            if ([1, 2, 3, 4].includes(region) && table.rows.length >= 12) {
                const firstRow = region >= 3 ? 6 : 0;
                const firstColumn = region % 2 === 0 ? 7 : 0;
                cells = [...table.rows].slice(firstRow, firstRow + 6).flatMap(row =>
                    [...row.cells].slice(firstColumn, firstColumn + 7)
                );
            } else {
                const bounds = block.getBoundingClientRect();
                cells = [...table.querySelectorAll('td')].filter(cell => {
                    const box = cell.getBoundingClientRect();
                    const x = (box.left + box.right) / 2;
                    const y = (box.top + box.bottom) / 2;
                    return box.width && box.height && x >= bounds.left && x < bounds.right
                        && y >= bounds.top && y < bounds.bottom;
                });
            }
            const matches = cells.filter(cell => !cell.matches('.pre-month, .next-month, .unselectable')
                && cell.innerText.trim() === String(target));
            return matches.length === 1 ? matches[0] : null;
        }""", expected.day)
        try:
            cell = day.as_element()
            if cell is None:
                raise RuntimeError("未能在目标月份唯一找到失效日期。")
            cell.click(timeout=6000)
        finally:
            day.dispose()

        time_ok = calendar.evaluate(r"""(picker, parts) => {
            const control = window.dorado?.widget?.Control?.findParentControl?.(picker, 'dorado.widget.DatePicker');
            const spinner = control?._timeSpinner;
            const value = parts.map(value => String(value).padStart(2, '0')).join(':');
            if (!spinner) {
                const inputs = [...picker.querySelectorAll('.spinner-block input')].filter(input => input.getClientRects().length);
                return inputs.length === 1 && inputs[0].value.trim() === value;
            }
            const [hours, minutes, seconds] = parts;
            const fields = {hours, minutes, seconds};
            if (!Object.entries(fields).every(([key, value]) => spinner.get(key) === value)) {
                spinner.set(fields);
            }
            return Object.entries(fields).every(([key, value]) => spinner.get(key) === value);
        }""", [expected.hour, expected.minute, expected.second])
        if not time_ok:
            raise RuntimeError("未能确认日历里的失效时分秒，未提交发码。")
        calendar.locator(".button-block").get_by_text(re.compile(r"^(确定|OK)$")).click(timeout=5000)
        calendar.wait_for(state="hidden", timeout=6000)
        values = trigger.evaluate(r"""trigger => {
            const control = window.dorado?.widget?.Control?.findParentControl?.(trigger, 'dorado.widget.DateTimeSpinner');
            const date = control?.get('value');
            if (date instanceof Date) {
                const pad = value => String(value).padStart(2, '0');
                return [[date.getFullYear(), pad(date.getMonth() + 1), pad(date.getDate())].join('-')
                    + ' ' + [date.getHours(), date.getMinutes(), date.getSeconds()].map(pad).join(':')];
            }
            for (let node = trigger.parentElement; node && !node.matches('.d-dialog, [role=dialog]'); node = node.parentElement) {
                const values = [...node.querySelectorAll('input')].map(input => input.value.trim())
                    .filter(value => /^\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$/.test(value));
                if (values.length) return values;
            }
            return [];
        }""")
        try:
            matches = len(values) == 1 and datetime.strptime(values[0].replace("/", "-"), "%Y-%m-%d %H:%M:%S") == expected
        except ValueError:
            matches = False
        if not matches:
            raise RuntimeError(f"失效时间未能确认为 {self.expire_date_str}，未提交发码。")

    def fill_generation_expiry(self, field):
        expected = datetime.fromisoformat(self.expire_date_str)
        input_type = field.get_attribute("type")
        value = self.expire_date_str
        if input_type == "datetime-local":
            value = expected.isoformat(timespec="seconds" if expected.second else "minutes")
        if input_type == "date":
            if expected.time() != datetime.min.time():
                raise RuntimeError("后台失效时间控件只支持日期，无法设置指定的时分秒。")
            value = expected.date().isoformat()

        def matches(value):
            try:
                value = value.strip()
                return (input_type == "date" or ":" in value) and datetime.fromisoformat(value) == expected
            except (ValueError, AttributeError):
                return False

        if field.is_editable():
            field.fill(value, timeout=5000)
            field.press("Tab")
        if matches(field.input_value()):
            return

        stored = field.evaluate("""(input, expiry) => {
            const component = window.dorado?.widget?.Component;
            if (typeof component?.get !== 'function') return null;
            let editor;
            for (let node = input; node && !node.matches('.d-dialog, [role=dialog]'); node = node.parentElement) {
                let candidate;
                try { candidate = component.get(node); } catch (_) { continue; }
                if (candidate?._textDom === input || candidate?.getDom?.() === input) {
                    editor = candidate;
                    break;
                }
            }
            if (typeof editor?.get !== 'function' || typeof editor.set !== 'function') return null;
            const actual = editor.get('value');
            const editorType = editor.constructor?.className || editor.constructor?.name || '';
            if (!(actual instanceof Date) && !/Date(?:Time)?(?:Editor|Picker)/i.test(editorType)) return null;
            const fields = expiry.split(/[- :]/).map(Number);
            const requested = new Date(fields[0], fields[1] - 1, ...fields.slice(2));
            if (actual?.getTime?.() !== requested.getTime()) {
                editor.set('value', requested);
                if (typeof editor.post === 'function') editor.post();
            }
            const stored = editor.get('value');
            if (!(stored instanceof Date)) return null;
            const pad = n => String(n).padStart(2, '0');
            return [stored.getFullYear(), pad(stored.getMonth() + 1), pad(stored.getDate())].join('-')
                + ' ' + [stored.getHours(), stored.getMinutes(), stored.getSeconds()].map(pad).join(':');
        }""", self.expire_date_str)
        if not matches(stored):
            raise RuntimeError(f"失效时间未能确认为 {self.expire_date_str}，未提交发码。")

    def activate(self, ctx, activity_id, checkpoint, state):
        self.select_activity(ctx, activity_id)
        self.wait_for_empty_activation_list(ctx)
        click_btn_by_text(ctx, "生成")
        deadline = time.monotonic() + 12
        dialog = None
        while time.monotonic() < deadline and dialog is None:
            for root in (ctx, ctx.page):
                candidate = root.locator(
                    "#d_dialogGenerateCode:visible, .d-dialog:visible:has-text('失效'), "
                    "[role=dialog]:visible:has-text('失效')"
                ).filter(has_text="数量").first
                if candidate.is_visible():
                    dialog = candidate
                    break
            if dialog is None:
                ctx.wait_for_timeout(200)
        if dialog is None:
            raise RuntimeError("点击生成后，未找到激活码生成窗口。")

        self.fill_generation_fields(dialog)
        submit = dialog.get_by_text(re.compile(r"^(确认|确定|提交)$"))
        if submit.count() != 1:
            raise RuntimeError("生成窗口未找到唯一的确认按钮。")
        self.log(f"提交发码：{self.code_count} 个，注册天数 0，失效时间 {self.expire_date_str}")
        checkpoint["submitted"] = True
        self.write_state(state)
        submit.click(timeout=10000)

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            self.exact_confirmation(ctx, "生成")
            for root in (ctx, ctx.page):
                success = root.locator(".d-message-box:visible, .d-dialog:visible").filter(
                    has_text=re.compile("操作成功|生成成功")
                ).first
                if success.is_visible():
                    okay = success.get_by_text("确定", exact=True)
                    if okay.count() == 1:
                        okay.click(timeout=3000)
            listing = self.activation_list(ctx)
            if not dialog.is_visible() and listing and (listing["populated"] or (listing["total"] or 0) > 0):
                return
            ctx.wait_for_timeout(250)
        raise RuntimeError("已提交生成，后台尚未显示激活码。重试时只会核对并导出已有记录。")

    def export(self, ctx, activity_id, tier):
        self.select_activity(ctx, activity_id)
        raw_directory = self.download_dir / "exports"
        raw_directory.mkdir(exist_ok=True)
        path = raw_directory / f"{tier}.xls"
        with ctx.page.expect_download(timeout=30000) as download:
            ctx.get_by_text(re.compile(r"^导出\s*Excel$")).first.click(timeout=10000)
        download.value.save_as(str(path))
        records = parse_single_export_file(str(path))
        validate_records(tier, records, self.code_count, self.expire_date_str)
        return records

    def execute(self, ctx, page):
        path = self.progress_dir / "progress.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        plan = self.job["plan"]
        pending = [f"{tier} 档 {row['name']}" for tier, value in plan["tiers"].items()
                   for row in value["items"] if row.get("status") == "review"]
        if pending:
            raise RuntimeError(f"以下奖励需要核对后才能配置：{'、'.join(pending)}")
        activities = {}
        for index, (tier, expected) in enumerate(self.job["rewards"].items()):
            self.set_progress(12 + index * 5, tier, "核对并配置档位")
            item = plan["tiers"][tier]
            checkpoint = state.setdefault(tier, {})
            if not checkpoint:
                self.select_activity(ctx, item["source"]["id"])
                if self.compare(expected, self.read_rewards(ctx)) != item["items"]:
                    raise RuntimeError(f"{tier} 档的往期奖励已变化，请重新预览")
            activity_id = self.ensure_target(ctx, item["source"], item["target"], checkpoint, state)
            if not checkpoint.get("submitted") and self.apply_rewards(ctx, activity_id, expected):
                page.reload(wait_until="domcontentloaded")
                ctx = self.open_gift_code_page(page)
            self.select_activity(ctx, activity_id)
            differences = [d for d in self.compare(expected, self.read_rewards(ctx)) if not d["matched"]]
            if differences:
                raise RuntimeError(f"{tier} 档保存后仍存在奖励差异：{', '.join(d['name'] for d in differences)}")
            activities[tier] = {"id": activity_id, "name": item["target"]}

        results = {}
        for index, (tier, activity) in enumerate(activities.items()):
            self.set_progress(54 + index * 5, tier, "核验并生成礼包码")
            activity_id = activity["id"]
            self.select_activity(ctx, activity_id)
            expected = self.job["rewards"][tier]
            if any(not item["matched"] for item in self.compare(expected, self.read_rewards(ctx))):
                raise RuntimeError(f"{tier} 档发码前校验发现后台奖励已经变化")
            checkpoint = state[tier]
            if not checkpoint.get("submitted"):
                self.activate(ctx, activity_id, checkpoint, state)
            results[tier] = self.export(ctx, activity_id, tier)
            self.log(f"{tier} 档核验完成，已导出 {len(results[tier])} 个礼包码")
        name = self.job["activity_name"]
        result = write_xls(self.download_dir, name, results, self.code_count, self.expire_date_str)
        completed = self.download_dir / "completed.json"
        temporary = completed.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "name": name, "batch_date": self.job["batch_date"], "activities": activities,
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(completed)
        return result

    def run(self, mode):
        session = Path(self.work_dir) / "session_state.json"
        with sync_playwright() as playwright:
            import sys
            is_headless = os.environ.get("HEADLESS", "").lower() in ("1", "true") or (sys.platform != "win32" and not os.environ.get("DISPLAY"))
            launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--window-size=1920,1080"]
            if not is_headless:
                launch_args.append("--start-maximized")
            browser = playwright.chromium.launch(headless=is_headless, slow_mo=0 if is_headless else 400, args=launch_args)
            try:
                options = {
                    "locale": "zh-CN",
                    "accept_downloads": True,
                    "viewport": {"width": 1920, "height": 1080},
                }
                if session.exists() and not self.job.get("credentials"):
                    try:
                        options["storage_state"] = json.loads(session.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
                context = browser.new_context(**options)
                page = context.new_page()
                try:
                    page.set_viewport_size({"width": 1920, "height": 1080})
                except Exception:
                    pass
                page.bring_to_front()
                self.login(page, context)
                ctx = self.open_gift_code_page(page)
                if self.job.get("credentials"):
                    try:
                        save_credentials(self.username, self.password)
                    except OSError as exc:
                        if getattr(exc, "winerror", None) != 1312:
                            raise
                        self.log("当前 Windows 会话无法保存密码，将继续使用已登录的 GMS 会话。", "WARN")
                return self.preview(ctx) if mode == "preview" else self.execute(ctx, page)
            finally:
                browser.close()
