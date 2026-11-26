# -*- coding: utf-8 -*-
"""
越南网页支付累充礼包码自动化配置引擎（HTTP 直连 GMS 后台版本）
- 直连 Dorado7 / BDF2 view-service 接口，毫秒级响应，告别浏览器 UI 自动化脆弱性
- 严格遵循业务规则：
  1. 生成数量严格设为 100；
  2. 注册天数限制保持默认 0，绝不更改；
  3. 失效时间精确设置为指定时间（如 2026-12-25 00:00:00）；
  4. 物品奖励修改时，物品编号（itemId）绝对不变，只按需修改数量；
  5. 往期基线严格按各档位与最新批次精准匹配；
  6. 接口签名、断点续传（progress.json）、单档位导出（exports/{tier}.xls）及最终合并（write_xls）完全兼容原工作台。
"""
import copy
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3
import xlwt

from credentials import load as load_credentials, save as save_credentials
from export_merger import parse_single_export_file, validate_records, write_xls
from unified_parser import TIERS, reward_name

urllib3.disable_warnings()

HISTORICAL_NAME = re.compile(r"越南[0-9]{4}网页支付累充\s*([0-9]+)礼包码([0-9]{8})")
WORKBENCH_NAME = re.compile(r"越南[0-9]{4}网页支付累充礼包码([0-9]{8})-([0-9]+)档")

LOADED_DATA_TYPES = [
    "NoviceCardV1",
    "ActivityReward",
    "Channel",
    "Item",
    "ChannelGiftMap",
    "NoviceCard",
    "Activity",
    "Condition",
]


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


def format_gms_time(val: Any) -> str:
    """将 GMS 返回的各种时间格式归一化为 YYYY-MM-DD HH:MM:SS"""
    if not val:
        return ""
    if isinstance(val, (int, float)):
        sec = val / 1000.0 if val > 1e12 else val
        return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).strip()
    s = s.replace("T", " ").replace("Z", "")
    if len(s) >= 19:
        return s[:19]
    if len(s) == 10 and "-" in s:
        return f"{s} 00:00:00"
    return s


def to_iso_expire_time(time_str: str) -> str:
    """将 YYYY-MM-DD HH:MM:SS 或类似时间转换为 Dorado 后台接收的 ISO 8601 格式 (YYYY-MM-DDTHH:MM:SSZ)"""
    s = str(time_str).strip()
    # 如果已经带 T 和 Z
    if "T" in s and s.endswith("Z"):
        return s
    # 如果只有日期 YYYY-MM-DD
    if len(s) == 10 and "-" in s:
        return f"{s}T00:00:00Z"
    # 标准格式 YYYY-MM-DD HH:MM:SS
    parts = s.split(" ")
    if len(parts) == 2:
        return f"{parts[0]}T{parts[1]}Z"
    return s


class GiftCodeAutomator:
    """基于 HTTP 直连 GMS 后台的礼包码自动化配置引擎"""

    def __init__(self, job, work_dir, log, progress, on_browser=None):
        self.job = job
        self.work_dir = Path(str(work_dir))
        self.log = log
        self.set_progress = progress
        self.on_browser = on_browser  # 保持原签名兼容
        self.base_url = "https://xzvn.forceforever.com/xz-vn-gms"
        self.view_url = f"{self.base_url}/com.ceapon.fire.gms.web.view.activity.NoviceCard.d"
        self.username, self.password = job.get("credentials") or load_credentials()
        self.download_dir = Path(job["directory"])
        self.progress_dir = Path(job.get("progress_directory", self.download_dir))
        self.expire_date_str = str(job["expire_time"]).strip()
        self.code_count = int(job.get("count", 100))
        self.session: Optional[requests.Session] = None

    # ==================== HTTP 直连与 Dorado7 通讯核心 ====================

    def _init_session(self):
        """初始化 HTTP 会话并设置请求头"""
        s = requests.Session()
        s.verify = False
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.session = s

    def login_http(self):
        """HTTP 直连登录 GMS 后台"""
        self.log(f"正在通过 HTTP 直连连接后台系统: {self.base_url}...")
        self.set_progress(5, "系统初始化", "正在连接后台系统...")
        self._init_session()

        login_url = f"{self.base_url}/login.jsp"
        check_url = f"{self.base_url}/security_check_"

        if not self.username or not self.password:
            raise RuntimeError("未配置后台账号和密码，请在工作台填写后重新启动。")

        self.log(f"使用配置的用户 [{self.username}] 提交免密身份验证...", "INFO")
        resp = self.session.post(
            check_url,
            data={
                "username_": self.username,
                "password_": self.password,
                "captcha_": "",
                "btn_Login": "登  录",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

        if "login.jsp" in str(resp.url) or 'id="username_"' in resp.text:
            raise RuntimeError("后台登录失败，请核对账号密码是否正确！")

        self.log("登录成功，正在加载普通激活码 View 状态与上下文...", "INFO")
        r_view = self.session.get(self.view_url, timeout=30)
        if r_view.status_code != 200:
            raise RuntimeError(f"普通激活码页面加载失败，HTTP 状态码: {r_view.status_code}")

        # 触发 Dorado 初始数据类型加载
        try:
            dt_payload = json.dumps({
                "action": "load-datatype",
                "dataType": ["v:com.ceapon.fire.gms.web.view.activity.NoviceCard$Condition"],
                "context": {},
            })
            self.session.post(
                f"{self.base_url}/dorado/view-service",
                data=dt_payload,
                headers={"Content-Type": "text/javascript", "Referer": self.view_url},
                timeout=10,
            )
        except Exception:
            pass

        self.log("成功进入后台系统并建立直连会话！", "SUCCESS")
        self.set_progress(10, "系统就绪", "成功进入后台系统")

    def post_view_service(self, inner_obj: Dict[str, Any]) -> Dict[str, Any]:
        """向 Dorado7 view-service 发送批量 XML/JSON 请求并解析响应"""
        if self.session is None:
            self.login_http()

        inner_json = json.dumps(inner_obj, ensure_ascii=False)
        body = f'<batch>\n<request type="json"><![CDATA[{inner_json}]]></request>\n</batch>'.encode("utf-8")
        headers = {
            "Content-Type": "text/xml",
            "Referer": self.view_url,
        }

        resp = self.session.post(
            f"{self.base_url}/dorado/view-service",
            data=body,
            headers=headers,
            timeout=40,
        )

        text = resp.text

        # 检查是否包含 Dorado 异常信息
        if "<exception" in text:
            # 提取异常提示
            msg = ""
            start = text.find("<![CDATA[")
            end = text.find("]]>", start)
            if start != -1 and end != -1:
                content = text[start + 9:end].strip()
                try:
                    err_json = json.loads(content)
                    msg = err_json.get("message") or content
                except Exception:
                    msg = content
            else:
                msg = text[:300]
            raise RuntimeError(f"GMS 后台接口返回错误: {msg}")

        start = text.find("<![CDATA[")
        end = text.find("]]>", start)
        if start == -1 or end == -1 or end <= start:
            # 会话失效重试
            self.log("检测到后台会话可能已过期，正在自动重登并重试...", "WARN")
            self.login_http()
            resp = self.session.post(
                f"{self.base_url}/dorado/view-service",
                data=body,
                headers=headers,
                timeout=40,
            )
            text = resp.text
            start = text.find("<![CDATA[")
            end = text.find("]]>", start)
            if start == -1 or end == -1:
                raise RuntimeError(f"GMS 接口返回非预期格式: {text[:200]}")

        payload = json.loads(text[start + 9:end].strip())
        if payload.get("exception"):
            err = payload["exception"]
            raise RuntimeError(err.get("message") or str(err))
        return payload

    # ==================== 业务直连接口 ====================

    def activity_list(self) -> List[Dict[str, Any]]:
        """HTTP 直连获取全部普通激活码活动列表"""
        req = {
            "action": "load-data",
            "dataProvider": "activityPR#loadActivities",
            "supportsEntity": True,
            "resultDataType": "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$[Activity]",
            "pageSize": 0,
            "pageNo": 1,
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        }
        res = self.post_view_service(req)
        raw_list = res.get("data") or []
        # 兼容原格式并附带 _raw 实体
        return [{"id": row.get("id"), "name": row.get("name") or "", "_raw": row} for row in raw_list]

    def read_rewards(self, activity_id: int) -> List[Dict[str, Any]]:
        """HTTP 直连读取指定活动的物品奖励列表（保证附带原实体，便于后续更新并保持 itemId 绝不变动）"""
        req = {
            "action": "load-data",
            "dataProvider": "activityPR#loadActivityRewards",
            "supportsEntity": True,
            "parameter": {"activityId": int(activity_id)},
            "resultDataType": "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$[ActivityReward]",
            "pageSize": 0,
            "pageNo": 1,
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        }
        res = self.post_view_service(req)
        raw_list = res.get("data") or []
        items = []
        for row in raw_list:
            items.append({
                "id": row.get("itemId"),
                "name": row.get("itemName") or "",
                "qty": row.get("count", 0),
                "_raw": row,
            })
        return items

    @staticmethod
    def compare(expected: Dict[str, int], actual: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """比对期望奖励与后台实际奖励"""
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
                results.append({
                    "id": item["id"],
                    "name": name,
                    "backend_name": item["name"],
                    "expected": quantity,
                    "actual": quantity,
                    "status": "same",
                    "matched": True,
                    "_raw": item.get("_raw"),
                })
            elif len(candidates) == 1:
                index, item = candidates[0]
                consumed.add(index)
                results.append({
                    "id": item["id"],
                    "name": name,
                    "backend_name": item["name"],
                    "expected": quantity,
                    "actual": item["qty"],
                    "status": "quantity",
                    "matched": False,
                    "_raw": item.get("_raw"),
                })
            else:
                review = "后台有多条同名奖励，无法确定要改哪条" if candidates else "后台找不到同名奖励"
                results.append({
                    "id": None,
                    "name": name,
                    "expected": quantity,
                    "actual": None,
                    "status": "review",
                    "matched": False,
                    "reason": review,
                })

        kept = (item for index, item in enumerate(actual) if index not in consumed)
        for item in sorted(kept, key=lambda item: (item["id"], item["name"], item["qty"])):
            results.append({
                "id": item["id"],
                "name": item["name"],
                "expected": None,
                "actual": item["qty"],
                "status": "kept",
                "matched": True,
                "_raw": item.get("_raw"),
            })
        return results

    def previous_batch(self, activities: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        """从活动列表中精准锁定上一期基线批次及完整的 8 个档位"""
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

    def target_name(self, tier: str) -> str:
        return f"{self.job['activity_name']}-{tier}档"

    def preview(self) -> Dict[str, Any]:
        """生成 8 个档位的配置对比预览数据"""
        self.login_http()
        baseline, selected = self.previous_batch(self.activity_list())
        self.log(f"本期 {self.job['batch_date']}，已锁定上一期基线 {baseline}，成功匹配 8 个来源档位！", "SUCCESS")

        preview = {}
        for index, (tier, expected) in enumerate(self.job["rewards"].items()):
            self.set_progress(10 + index * 10, tier, f"读取档位 [{tier}] 往期奖励...")
            source = selected[tier]
            rewards = self.read_rewards(source["id"])
            diff = self.compare(expected, rewards)
            preview[tier] = {
                "source": {"id": source["id"], "name": source["name"]},
                "target": self.target_name(tier),
                "items": diff,
            }
        return {"batch_date": self.job["batch_date"], "baseline": baseline, "tiers": preview}

    def write_state(self, state: Dict[str, Any]):
        """持久化保存任务断点状态"""
        path = self.progress_dir / "progress.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    # ==================== 执行操作（拷贝、改名、改奖励、发码、导出） ====================

    def copy_activity(self, source: Dict[str, Any], target_name: str, checkpoint: Dict[str, Any], state: Dict[str, Any]) -> int:
        """HTTP 直连调用 activityPR#copyGiftPackagePersonal 拷贝活动"""
        self.log(f"正在拷贝上一期活动 [ID: {source['id']}] {source['name']}...", "INFO")
        before_list = self.activity_list()
        before_ids = {row["id"] for row in before_list}

        # 调用后台拷贝服务
        self.post_view_service({
            "action": "remote-service",
            "service": "activityPR#copyGiftPackagePersonal",
            "parameter": {"id": int(source["id"])},
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        })
        time.sleep(1)

        # 重新拉取活动列表寻找新生成的副本
        after_list = self.activity_list()
        copy_names = (source["name"] + "(copy)", source["name"] + " (copy)")
        new_copies = [r for r in after_list if r["id"] not in before_ids and (r["name"] in copy_names or "(copy)" in r["name"])]

        if not new_copies:
            # 兜底按 ID 最大排序
            candidates = [r for r in after_list if r["id"] not in before_ids]
            if candidates:
                new_copies = sorted(candidates, key=lambda x: x["id"], reverse=True)

        if not new_copies:
            raise RuntimeError(f"未能确认拷贝活动生成的副本记录！")

        activity_id = new_copies[0]["id"]
        checkpoint["id"] = activity_id
        self.write_state(state)
        self.log(f"新活动副本已就绪 (活动编号: {activity_id})", "SUCCESS")
        return activity_id

    def ensure_target(self, source: Dict[str, Any], name: str, checkpoint: Dict[str, Any], state: Dict[str, Any]) -> int:
        """确保目标活动创建并重命名完毕"""
        activities = self.activity_list()
        existing = [row for row in activities if row["name"] == name]
        activity_id = checkpoint.get("id")

        if existing:
            if len(existing) == 1 and (activity_id is None or existing[0]["id"] == activity_id):
                checkpoint["id"] = existing[0]["id"]
                self.write_state(state)
                return existing[0]["id"]
            raise RuntimeError(f"后台已有同名活动「{name}」，请确保活动名称唯一")

        if activity_id is None:
            activity_id = self.copy_activity(source, name, checkpoint, state)

        # 获取当前副本实体的完整数据进行重命名
        activities_now = self.activity_list()
        target_row = next((r for r in activities_now if r["id"] == activity_id), None)
        if not target_row:
            raise RuntimeError(f"未能在后台列表中找到新拷贝的活动 ID {activity_id}")

        self.log(f"重命名活动 [ID: {activity_id}] 为: [{name}]...", "INFO")
        raw_entity = target_row["_raw"]
        old_entity = copy.deepcopy(raw_entity)
        mod_entity = copy.deepcopy(raw_entity)
        mod_entity["$dataType"] = "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$Activity"
        mod_entity["$state"] = 2
        mod_entity["$oldData"] = old_entity
        mod_entity["name"] = name

        self.post_view_service({
            "action": "resolve-data",
            "dataResolver": "activityPR#saveActivity",
            "dataItems": [{"data": [mod_entity], "dataType": "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$[Activity]"}],
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        })
        time.sleep(0.5)

        # 验证重命名结果
        verified_list = self.activity_list()
        verified = next((r for r in verified_list if r["id"] == activity_id and r["name"] == name), None)
        if not verified:
            raise RuntimeError(f"保存后未能确认活动名称已更新为「{name}」")

        self.log(f"活动成功就绪并重命名为: [{name}]", "SUCCESS")
        return activity_id

    def edit_reward_quantity(self, change: Dict[str, Any]):
        """
        HTTP 直连修改物品奖励数量
        【严格规范】：原档位礼包码里面的物品编号（itemId）绝对不变，只将 count 更新为目标值！
        """
        name = change["name"]
        raw = change.get("_raw")
        if not raw:
            raise RuntimeError(f"缺少「{name}」的原实体数据，无法执行安全更新。")

        item_id = raw.get("itemId")
        expected_qty = change["expected"]

        self.log(f"正在更新物品 [{name}] 数量: {change.get('actual')} -> {expected_qty} (物品编号 itemId={item_id} 严格保持不变)...", "INFO")

        old_r = copy.deepcopy(raw)
        new_r = copy.deepcopy(raw)
        new_r["$dataType"] = "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$ActivityReward"
        new_r["$state"] = 2
        new_r["$oldData"] = old_r
        new_r["count"] = expected_qty
        # 强制锁死 itemId
        new_r["itemId"] = item_id

        self.post_view_service({
            "action": "resolve-data",
            "dataResolver": "activityPR#saveActivityReward",
            "dataItems": [{"data": [new_r], "dataType": "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$[ActivityReward]"}],
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        })
        self.log(f"物品 [{name}] (编号: {item_id}) 数量已成功更新为 {expected_qty}！", "SUCCESS")

    def apply_rewards(self, activity_id: int, expected: Dict[str, int]) -> bool:
        """检查并按需更新物品奖励"""
        rewards = self.read_rewards(activity_id)
        comparison = self.compare(expected, rewards)
        pending = [item["name"] for item in comparison if item["status"] == "review"]
        if pending:
            raise RuntimeError(f"无法在现有奖励中定位：{'、'.join(pending)}")

        changes = [item for item in comparison if item["status"] == "quantity"]
        for change in changes:
            self.edit_reward_quantity(change)
        return bool(changes)

    def activate(self, activity_id: int, checkpoint: Dict[str, Any], state: Dict[str, Any]):
        """
        HTTP 直连生成 100 个激活码
        【核心约束强制保证】：
          1. 数量 count 严格为 100（self.code_count）；
          2. 注册天数 regsterDays 严格保持 0；
          3. 失效时间 expireTime 严格为用户设定的时间（转为 ISO 格式提交）。
        """
        # 1. 检查当前是否已生成激活码
        req_check = {
            "action": "load-data",
            "dataProvider": "activityPR#loadNoviceCardsV1",
            "supportsEntity": True,
            "parameter": {"activityId": int(activity_id)},
            "resultDataType": "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$[NoviceCardV1]",
            "pageSize": 10,
            "pageNo": 1,
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        }
        res_check = self.post_view_service(req_check)
        existing_data = res_check.get("data", {})
        existing_cards = existing_data.get("data", []) if isinstance(existing_data, dict) else existing_data

        if existing_cards:
            self.log(f"检测到当前活动已包含 {len(existing_cards)} 笔激活码记录，直接使用已有记录，避免重复生成。", "INFO")
            checkpoint["submitted"] = True
            self.write_state(state)
            return

        iso_expire = to_iso_expire_time(self.expire_date_str)
        self.log(f"提交生成激活码参数: [生成数量={self.code_count}], [注册天数限制=0 (强制锁死默认)], [失效时间={self.expire_date_str}]...", "INFO")

        req_gen = {
            "action": "remote-service",
            "service": "activityPR#generateCode",
            "parameter": {
                "count": self.code_count,
                "activityId": int(activity_id),
                "regsterDays": 0,
                "expireTime": iso_expire,
            },
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        }
        res_gen = self.post_view_service(req_gen)
        if res_gen.get("data") != 1:
            raise RuntimeError(f"生成激活码未返回成功标志，后台返回: {res_gen}")

        self.log("激活码生成成功！", "SUCCESS")
        checkpoint["submitted"] = True
        self.write_state(state)

    def export(self, activity_id: int, tier: str) -> List[Dict[str, str]]:
        """
        HTTP 直连获取生成的全部 100 个激活码，并导出保存单档位 xls 文件
        """
        raw_directory = self.download_dir / "exports"
        raw_directory.mkdir(parents=True, exist_ok=True)
        path = raw_directory / f"{tier}.xls"

        req_cards = {
            "action": "load-data",
            "dataProvider": "activityPR#loadNoviceCardsV1",
            "supportsEntity": True,
            "parameter": {"activityId": int(activity_id)},
            "resultDataType": "v:com.ceapon.fire.gms.web.view.activity.NoviceCard$[NoviceCardV1]",
            "pageSize": self.code_count,
            "pageNo": 1,
            "context": {},
            "loadedDataTypes": LOADED_DATA_TYPES,
        }
        res_cards = self.post_view_service(req_cards)
        card_data = res_cards.get("data", {})
        cards = card_data.get("data", []) if isinstance(card_data, dict) else card_data

        if len(cards) < self.code_count:
            # 容错重试一次
            time.sleep(1)
            res_cards = self.post_view_service(req_cards)
            card_data = res_cards.get("data", {})
            cards = card_data.get("data", []) if isinstance(card_data, dict) else card_data

        records = []
        for card in cards:
            c_code = str(card.get("code") or "").strip()
            c_create = format_gms_time(card.get("createtime"))
            c_expire = format_gms_time(card.get("expireTime"))
            if c_code:
                records.append({
                    "code": c_code,
                    "created_time": c_create,
                    "expired_time": c_expire,
                })

        # 严格验证数据完整性与失效时间一致性
        validate_records(tier, records, self.code_count, self.expire_date_str)

        # 写入标准的单档位 exports/{tier}.xls
        book = xlwt.Workbook()
        sheet = book.add_sheet(tier)
        header = xlwt.easyxf("font: bold on; align: horiz center;")
        headers = ["批次号", "激活码", "状态", "创建时间", "服务器名称", "角色ID", "角色名", "领取时间", "注册天数限制", "失效时间"]
        for col_idx, h_text in enumerate(headers):
            sheet.write(0, col_idx, h_text, header)

        for row_idx, card in enumerate(cards, start=1):
            sheet.write(row_idx, 0, str(card.get("batchNo") or ""))
            sheet.write(row_idx, 1, str(card.get("code") or ""))
            sheet.write(row_idx, 2, str(card.get("status") if card.get("status") is not None else 0))
            sheet.write(row_idx, 3, format_gms_time(card.get("createtime")))
            sheet.write(row_idx, 4, str(card.get("serverName") or ""))
            sheet.write(row_idx, 5, str(card.get("pid") if card.get("pid") is not None else 0))
            sheet.write(row_idx, 6, str(card.get("playerName") or ""))
            sheet.write(row_idx, 7, format_gms_time(card.get("usetime")))
            sheet.write(row_idx, 8, str(card.get("regsterDays") if card.get("regsterDays") is not None else 0))
            sheet.write(row_idx, 9, format_gms_time(card.get("expireTime")))

        temp_path = path.with_suffix(".tmp")
        book.save(str(temp_path))
        temp_path.replace(path)

        return records

    def execute(self) -> str:
        """执行完整自动化任务（创建活动、修改奖励、发码、导出汇总）"""
        self.login_http()

        path = self.progress_dir / "progress.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        plan = self.job["plan"]

        pending = [
            f"{tier} 档 {row['name']}"
            for tier, value in plan["tiers"].items()
            for row in value["items"]
            if row.get("status") == "review"
        ]
        if pending:
            raise RuntimeError(f"以下奖励需要核对后才能配置：{'、'.join(pending)}")

        # 阶段 1：创建并配置各档位活动
        activities = {}
        for index, (tier, expected) in enumerate(self.job["rewards"].items()):
            self.set_progress(12 + index * 5, tier, f"正在核对并就绪档位 [{tier}]...")
            item = plan["tiers"][tier]
            checkpoint = state.setdefault(tier, {})

            if not checkpoint:
                current_rewards = self.read_rewards(item["source"]["id"])
                if self.compare(expected, current_rewards) != item["items"]:
                    raise RuntimeError(f"{tier} 档的往期奖励已变化，请重新预览")

            activity_id = self.ensure_target(item["source"], item["target"], checkpoint, state)

            if not checkpoint.get("submitted"):
                self.apply_rewards(activity_id, expected)

            # 严格复核奖励是否 100% 一致
            current_after = self.read_rewards(activity_id)
            differences = [d for d in self.compare(expected, current_after) if not d["matched"]]
            if differences:
                raise RuntimeError(f"{tier} 档保存后仍存在奖励差异：{', '.join(d['name'] for d in differences)}")

            self.log(f"✅ 档位 [{tier}] 奖励配置核验通过：与上传表格配置 100% 一致！", "SUCCESS")
            activities[tier] = {"id": activity_id, "name": item["target"]}

        # 阶段 2：逐档发码并导出
        results = {}
        for index, (tier, activity) in enumerate(activities.items()):
            self.set_progress(54 + index * 5, tier, f"正在生成并导出档位 [{tier}] 礼包码...")
            activity_id = activity["id"]
            checkpoint = state[tier]

            # 发码前再次复核奖励
            current_before_gen = self.read_rewards(activity_id)
            if any(not item["matched"] for item in self.compare(self.job["rewards"][tier], current_before_gen)):
                raise RuntimeError(f"{tier} 档发码前校验发现后台奖励已经变化！")

            if not checkpoint.get("submitted"):
                self.activate(activity_id, checkpoint, state)

            results[tier] = self.export(activity_id, tier)
            self.log(f"档位 [{tier}] 成功导出 {len(results[tier])} 个礼包码！", "SUCCESS")

        # 阶段 3：合并生成单一汇总文件并记录完成
        self.set_progress(95, "合并导出", "正在生成最终多档位汇总表格...")
        name = self.job["activity_name"]
        final_file = write_xls(self.download_dir, name, results, self.code_count, self.expire_date_str)

        completed = self.download_dir / "completed.json"
        temporary = completed.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "name": name,
                    "batch_date": self.job["batch_date"],
                    "activities": activities,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(completed)

        self.set_progress(100, "完成", "所有档位礼包码生成并导出完成！")
        self.log(f"🎉 全部 8 个档位礼包码自动化配置完成！汇总文件已保存至: {final_file}", "SUCCESS")
        return final_file

    def run(self, mode: str):
        """外部统一入口"""
        if self.job.get("credentials"):
            try:
                save_credentials(self.username, self.password)
            except OSError as exc:
                if getattr(exc, "winerror", None) != 1312:
                    raise
                self.log("当前 Windows 会话无法保存密码，将继续使用已验证的 GMS 会话。", "WARN")

        if mode == "preview":
            return self.preview()
        elif mode == "execute":
            return self.execute()
        else:
            raise ValueError(f"未知运行模式: {mode}")
