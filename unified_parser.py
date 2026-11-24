import re
from pathlib import Path
from unicodedata import normalize

import docx
import pandas as pd

TIERS = ("100", "200", "300", "500", "1000", "2000", "5000", "10000")
ALIASES = {"飞升卡宝箱": "飞升卡资源宝箱", "飞升月卡宝箱": "飞升卡资源宝箱"}
REWARD = re.compile(r"([^xX×*，,；;、]+?)\s*[xX×*]\s*(\d+?)(?=\s*[^0-9]|$|(?!\d))")


def reward_name(value):
    name = "".join(normalize("NFKC", value).split())
    return ALIASES.get(name, name)


def normalize_tier(text):
    value = str(text).strip()
    if re.fullmatch(r"\d+\.0+", value):
        value = value.partition(".")[0]
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", value):
        value = re.sub(r"[.,]", "", value)
    if not value.isdecimal():
        return ""
    number = int(value)
    return str(number // 1000) if number >= 100000 and number % 1000 == 0 else str(number)


def parse_rewards(text):
    rewards = {}
    for line in str(text).splitlines():
        if not line.strip():
            continue
        line = re.sub(r"([xX×*]\s*\d+)([1-9]级)", r"\1 \2", line)
        offset = 0
        for match in REWARD.finditer(line):
            if line[offset:match.start()].strip(" \t，,；;、"):
                raise ValueError(f"无法识别奖励：{line}")
            name = reward_name(match[1])
            count = int(match[2])
            if not name or count <= 0:
                raise ValueError(f"道具「{name}」的数量必须大于零")
            if name in rewards:
                raise ValueError(f"道具「{name}」重复")
            rewards[name] = count
            offset = match.end()
        if line[offset:].strip(" \t，,；;、"):
            raise ValueError(f"无法识别奖励：{line}")
    if not rewards:
        raise ValueError("奖励为空")
    return rewards


def parse_text_rows(text):
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2:
            yield cells[:2]


def parse_any_file(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        doc = docx.Document(str(path))
        rows = ([cell.text for cell in row.cells[:2]] for table in doc.tables for row in table.rows)
    elif suffix in (".xls", ".xlsx", ".csv"):
        frame = pd.read_csv(path, dtype=str) if suffix == ".csv" else pd.read_excel(path, dtype=str)
        rows = frame.iloc[:, :2].itertuples(index=False, name=None)
    elif suffix == ".txt":
        rows = parse_text_rows(Path(path).read_text(encoding="utf-8-sig"))
    else:
        raise ValueError("请选择 DOCX、XLSX、XLS、CSV 或 TXT 文件")
    return parse_tiers(rows)


def parse_tiers(rows):
    result = {}
    for row in rows:
        if len(row) < 2:
            continue
        tier = normalize_tier(row[0])
        if not tier:
            continue
        if tier not in TIERS:
            raise ValueError(f"未知档位：{row[0]}")
        if tier in result:
            raise ValueError(f"{tier} 档位重复")
        try:
            result[tier] = parse_rewards(row[1])
        except ValueError as exc:
            raise ValueError(f"{tier} 档：{exc}") from exc
    missing = [tier for tier in TIERS if tier not in result]
    if missing:
        raise ValueError(f"缺少档位：{'、'.join(missing)}")
    return {tier: result[tier] for tier in TIERS}
