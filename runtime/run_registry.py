# -*- coding: utf-8 -*-
"""run 目录发现与角色判定。

下游消费者（客户视图页面、对照报告、一键重建）必须按「输入快照 + 计算语义 +
生成时间」定位 run，而**不能把某个 run 的目录名写死在代码里**。

原因：run 目录名由 `writers.new_run_id()` 按时间戳生成，重跑必然换名。
一旦代码里写死 `v2-base-20261119` 这类目录名，「清掉缓存后重跑」就会在
页面构建阶段直接 FileNotFoundError——页面从「skill 的派生产物」退化成
「绑死在某一次运行结果上的快照」。

本模块给出的定位键是三段式，彼此独立：

* ``role``：由输入快照的 ``meta.official_anchor`` 判定——带官方锚定字段的是
  ``anchor``，纯声明式的是 ``base``。这是**输入事实**，不依赖目录名。
* ``semantics_version``：计算口径版本。v1 与 v2 产物同名共存时，必须按语义
  版本过滤，否则会拿 v1 的数值当成 v2 的结论。
* ``generated_at``：同一 (role, semantics_version) 下取最新一次运行。

三条规则任一条缺失都不猜：找不到就报明确错误，并列出实际可用的 run。
"""
from __future__ import annotations

import json
from pathlib import Path

ROLE_BASE = "base"
ROLE_ANCHOR = "anchor"

# 角色判定的回退依据：仅当快照缺 official_anchor 字段时使用 data_version 前缀。
_ANCHOR_PREFIX = "hybrid-"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def classify(run_dir: Path) -> str | None:
    """判定 run 的角色；无法判定返回 None。"""
    snap = _read_json(run_dir / "input_snapshot.json")
    meta = snap.get("meta") if isinstance(snap.get("meta"), dict) else {}
    if not meta:
        return None
    if meta.get("official_anchor"):
        return ROLE_ANCHOR
    version = str(meta.get("data_version") or "")
    if version.startswith(_ANCHOR_PREFIX):
        return ROLE_ANCHOR
    return ROLE_BASE


def iter_runs(runs_dir: Path) -> list[dict]:
    """列出全部可用 run 及其定位键，按 (role, generated_at) 排序。"""
    if not runs_dir.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir() or not (child / "run_manifest.json").is_file():
            continue
        manifest = _read_json(child / "run_manifest.json")
        snap = _read_json(child / "input_snapshot.json")
        meta = snap.get("meta") if isinstance(snap.get("meta"), dict) else {}
        out.append({
            "id": child.name,
            "path": child,
            "role": classify(child),
            "data_version": manifest.get("data_version") or meta.get("data_version"),
            "semantics_version": manifest.get("semantics_version"),
            "source_mode": manifest.get("source_mode") or meta.get("source_mode"),
            "generated_at": str(manifest.get("generated_at") or ""),
        })
    out.sort(key=lambda r: (r["role"] or "", r["generated_at"], r["id"]))
    return out


def _pick(candidates: list[dict], semantics: str | None) -> dict | None:
    """同组内取最新一次运行。

    指定了语义版本时**不回退**：找不到该语义版本的 run 就返回 None。
    静默回退到 v1 历史产物会让人把旧口径的数值当成当前结论 —— 这类
    「找不到就降级取别的」比直接报缺失更危险。
    """
    if not candidates:
        return None
    if semantics:
        matched = [c for c in candidates if c["semantics_version"] == semantics]
        return matched[-1] if matched else None
    return candidates[-1]


def resolve(runs_dir: Path, base: str | None = None, anchor: str | None = None,
            semantics: str | None = None) -> dict:
    """定位 base / anchor 两个 run。

    显式指定（``base`` / ``anchor``）优先；否则按 role + semantics + 生成时间
    自动发现。返回值里 ``resolved`` 记录每个角色的选择依据，便于在产物里留痕。
    """
    runs = iter_runs(runs_dir)
    by_id = {r["id"]: r for r in runs}

    def _explicit(name: str | None) -> dict | None:
        if not name:
            return None
        path = Path(name)
        if path.is_absolute() and path.is_dir():
            return {"id": path.name, "path": path, "role": classify(path),
                    "data_version": None, "semantics_version": None,
                    "source_mode": None, "generated_at": ""}
        return by_id.get(name)

    resolved: dict = {}
    for role, given in ((ROLE_BASE, base), (ROLE_ANCHOR, anchor)):
        chosen = _explicit(given)
        how = "explicit" if chosen else "auto"
        if chosen is None:
            pool = [r for r in runs if r["role"] == role]
            chosen = _pick(pool, semantics)
        resolved[role] = {
            "id": chosen["id"] if chosen else None,
            "path": chosen["path"] if chosen else None,
            "how": how if chosen else "missing",
            "data_version": chosen["data_version"] if chosen else None,
            "semantics_version": chosen["semantics_version"] if chosen else None,
        }
    resolved["available"] = [
        {"id": r["id"], "role": r["role"], "data_version": r["data_version"],
         "semantics_version": r["semantics_version"], "generated_at": r["generated_at"]}
        for r in runs
    ]
    return resolved


def describe_missing(resolved: dict) -> str:
    """给出一条能直接照做的错误提示，列出实际可用的 run。"""
    lines = []
    for role in (ROLE_BASE, ROLE_ANCHOR):
        info = resolved.get(role) or {}
        if info.get("id"):
            continue
        lines.append(f"  - 缺少 {role} run：可用 run 如下"
                     f"（role / data_version / semantics_version）：")
        for item in resolved.get("available", []):
            lines.append(f"      {item['id']}  role={item['role']}  "
                         f"data_version={item['data_version']}  "
                         f"semantics_version={item['semantics_version']}")
    if not lines:
        return ""
    return ("run 自动发现失败：\n" + "\n".join(lines) +
            "\n请先执行 `python scripts/rebuild_all.py`，或用 --base/--anchor 显式指定。")


if __name__ == "__main__":  # pragma: no cover - 便于人工排查
    import sys
    root = Path(__file__).resolve().parent.parent
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "reports" / "runtime_out"
    info = resolve(target)
    print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
