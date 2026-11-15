#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接着中断点采完三个渠道的会员权益分页数据，并产出对账用的三份 CSV。

设计要点
--------
1. 续抓而不是重抓：raw/ 下的快照页与 crawl_state.json 记录的进度直接复用，
   只对缺失页调用 api.fetch；已齐则第二次运行不再产生任何接口调用，输出保持不变。
2. 翻页以接口返回的 more 为准，不用 total 推算页数（数据在动，total 会漂移）。
3. 配额被拒（fetch 抛错）时记入 rejects.csv 并停止该渠道继续翻页：被拒本身也
   消耗一次配额，立刻重试只会再次被拒。
4. 同一 (mid, channel, benefit) 在多页（或快照与续抓之间）重复出现时只留一行，
   取 updated_at 最新的一行；updated_at 相同则保留采集顺序中先出现的一行。
   去重前先按状态过滤，只有“生效/冻结”算在持（“已用/过期”为终态，不计）。
"""

import csv
import json
import os
import sys

# -I 隔离模式下脚本目录不会自动进 sys.path，显式加入以便 import api
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api

CHANNELS = ("points", "grade", "paid")
HOLD_STATUSES = {"生效", "冻结"}
CHANNEL_ORDER = {ch: i for i, ch in enumerate(CHANNELS)}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE_DIR, "raw")
STATE_PATH = os.path.join(BASE_DIR, "crawl_state.json")
HOLDINGS_PATH = os.path.join(BASE_DIR, "holdings.csv")
ROLLUP_PATH = os.path.join(BASE_DIR, "channel_rollup.csv")
REJECTS_PATH = os.path.join(BASE_DIR, "rejects.csv")
NOTES_PATH = os.path.join(BASE_DIR, "notes.txt")


def snapshot_path(channel, page):
    return os.path.join(RAW_DIR, "%s_%d.json" % (channel, page))


def load_state():
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    return {ch: int(state.get(ch, 0)) for ch in CHANNELS}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")


def read_snapshot(channel, page):
    """读一页快照；文件缺失或内容损坏返回 None（按未抓到处理）。"""
    try:
        with open(snapshot_path(channel, page), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "rows" not in data:
        return None
    return data


def write_snapshot(channel, page, payload):
    with open(snapshot_path(channel, page), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")


def crawl_channel(channel, done_pages, state, rejects):
    """确保把一个渠道从头翻到 more=False。

    返回 (pages, total, done_pages)：
      pages 是按页号 1..N 排好的全部成功取得的页 payload（快照 + 续抓）；
      total 取第一页返回的 total。
    过程中被拒的调用追加进 rejects；每续抓成功一页立即落快照并推进中断点。
    """
    pages = []
    total = None
    page = 1
    while True:
        payload = read_snapshot(channel, page) if page <= done_pages else None
        if payload is None:
            try:
                payload = api.fetch(channel, page)
            except Exception:
                # 配额被拒（或其它接口异常）：记录后停止本渠道翻页。
                # 被拒同样消耗配额，重试没有意义。
                rejects.append((channel, page))
                break
            write_snapshot(channel, page, payload)
            done_pages = page
            state[channel] = done_pages
            save_state(state)

        pages.append(payload)
        if total is None:
            total = payload.get("total")
        if not payload.get("more"):
            break
        page += 1
    return pages, total, done_pages


def pick_holder(existing, candidate):
    """同一权益多行时的取舍：updated_at 较新者胜；相同则保留先到的一行。"""
    if existing is None:
        return candidate
    if candidate["updated_at"] > existing["updated_at"]:
        return candidate
    return existing


def main():
    state = load_state()
    rejects = []
    rollup_rows = []
    # key=(mid, channel, benefit) -> 选中的行
    holders = {}

    for channel in CHANNELS:
        pages, total, _ = crawl_channel(
            channel, state.get(channel, 0), state, rejects
        )
        for payload in pages:
            for row in payload.get("rows", []):
                if row.get("status") not in HOLD_STATUSES:
                    continue
                key = (row["mid"], channel, row["benefit"])
                holders[key] = pick_holder(holders.get(key), row)

        holdings_count = sum(1 for key in holders if key[1] == channel)
        rollup_rows.append((channel, len(pages), holdings_count, total))

    # 渠道顺序 points/grade/paid，渠道内按 mid、benefit 排序，
    # 保证同样输入下输出逐字节稳定（重跑幂等）。
    holding_records = [
        (
            mid,
            channel,
            benefit,
            row["status"],
            row["amount"],
            row["updated_at"],
        )
        for (mid, channel, benefit), row in sorted(
            holders.items(), key=lambda kv: (CHANNEL_ORDER[kv[0][1]], kv[0][0], kv[0][2])
        )
    ]

    with open(HOLDINGS_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mid", "channel", "benefit", "status", "amount", "updated_at"])
        w.writerows(holding_records)

    with open(ROLLUP_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel", "polled", "holdings", "size"])
        for channel, polled, holdings_count, total in rollup_rows:
            w.writerow([channel, polled, holdings_count, total])

    with open(REJECTS_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel", "page"])
        w.writerows(rejects)

    with open(NOTES_PATH, "w", encoding="utf-8") as f:
        f.write(NOTES)


NOTES = """采集与去重说明
================

1. 续抓范围
   raw/ 下的页快照与 crawl_state.json 是上次中断时的进度，本次直接复用，
   只对缺失页调用 api.fetch；翻页以接口返回的 more 为结束依据，不按 total
   推算页数，避免数据变动导致的页数漂移。每抓到一页立即落盘快照并更新
   crawl_state.json，再次中断后仍可继续。

2. 在持口径
   status 为“生效”“冻结”的权益算在持，计入 holdings.csv；
   “已用”“过期”是终态，不计入。

3. 同一权益多行的取舍
   同一 (mid, channel, benefit) 在相邻页、或快照与续抓数据之间可能重复出现，
   且 status、updated_at 可能不同。先按状态过滤出在持行，同一权益只保留一行，
   规则为：取 updated_at 最新的一行（updated_at 为 YYYY-MM-DD HH:MM:SS 文本，
   字典序与时间先后一致）；updated_at 相同时保留先采集到的一行，结果确定。
   若某权益在采集窗口内已全部变为终态（已用/过期），则不计入 holdings。

4. 被拒调用
   api.fetch 抛错即视为该次调用被配额拒绝，记入 rejects.csv（channel,page），
   并停止该渠道后续翻页——被拒调用本身也消耗配额，立即重试只会再次被拒。
   没有被拒时 rejects.csv 只有表头。

5. 幂等
   全部页都已在 raw/ 中有快照且末页 more=false 时，再次运行 collect.py
   不会调用 api.fetch，三份 CSV 与本文件内容保持不变。
   channel_rollup.csv 中：polled = 该渠道成功取得的页数（快照 + 续抓）；
   holdings = holdings.csv 中该渠道的去重后在持行数；
   size = 该渠道第一页返回的 total。
"""


if __name__ == "__main__":
    main()
