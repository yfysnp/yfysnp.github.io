#!/usr/bin/env bash
# 巡检器回归素材采集：从生产机上把"某几个群"的现场打成一个包，拿回本机回放判定。
#
# ⚠️ 这个脚本原本是 openclaw 上游的故障诊断工具（openclaw-diagnostic/collect.sh，
#    配套 viewer.html）。它只认旧的 agents/*/sessions/sessions.json 布局，
#    2026.9.2 会话搬进 SQLite 之后会产出**零会话的空包**，所以整个采集逻辑重写了：
#      · 数据源从 sessions.json + *.jsonl 换成 agents/*/agent/openclaw-agent.sqlite
#      · 收集范围对齐巡检器真正会读的那几处盘（见下面「包里装什么」）
#      · Node 换成 Python —— 要用 sqlite3 的 backup API 做一致性快照
#    上游那份原件仍在 ~/.openclaw/runtime/config-baseline/.openclaw/tools/
#    openclaw-diagnostic/ 下，需要给 openclaw 官方排障时用那一份、不要用这个。
#
# 包里装什么（每一项都对应巡检器的一处判据，缺了对应判据静默失效）：
#
#   openclaw/<实例>/openclaw.json                       实例判据（**已脱敏**）
#   openclaw/<实例>/agents/<agent>/agent/*.sqlite       会话/消息流/trajectory
#   openclaw/<实例>/private-experts/<da>/workflow/      交付数字人判据
#   openclaw/contexts/groups/<群号>/                    功能 24 编排层 state.json
#   openclaw/projects/<项目>/META.md                    步骤 0.5 结项闸门
#   openclaw/cc-config/projects/<编码目录>/session.json 信号 4（当前禁用，一并收）
#
# 包内目录**逐层镜像 ~/.openclaw**，所以解包后可以直接当数据根用：
#   python3 offline_replay.py <解包目录>/openclaw <群号> --da <数字人>
#
# ⚠️ 关于"只要这个群" —— 做不到。SQLite 布局下一个 agent 的**所有**群会话都在同一个
#    库里，没法像旧布局那样按文件挑。这里只做到"只拷参与了这些群的 agent 的库"，
#    库内其他群的会话内容仍在包里。已确认接受这个代价（整库拷零风险、库能直接用）。
#    因此包里含真实群聊内容，**严禁入库**，传输和留存都按敏感数据对待。
#
# 用法：
#   ./collect.sh <群号> [<群号> ...] [--out <目录>] [--openclaw-home <目录>]
#                                   [--include-workspaces] [--no-redact]
set -euo pipefail

usage() {
  cat <<'EOF'
用法:
  collect.sh <群号> [<群号> ...] [选项]

示例:
  ./collect.sh 10233933793
  ./collect.sh 10233933793 10233880975 --out ~/Desktop
  ./collect.sh 10233933793 --openclaw-home /data/openclaw

选项:
  --out <目录>             zip 输出目录，默认当前目录
  --openclaw-home <目录>   openclaw 家目录，默认 ~/.openclaw
  --include-workspaces     额外收 AGENTS.md 和 skills/（体积大，排查上下文时才要）
  --no-redact              不脱敏 openclaw.json（默认会把 token/密钥类字段打码）
  -h, --help               显示本帮助

产物:
  openclaw-inspector-<群号>-<时间戳>.zip
  解包后可直接回放:
    python3 offline_replay.py <解包目录>/openclaw <群号> --da <数字人>

注意:
  包里含真实群聊内容（SQLite 整库拷贝，含库内其他群的会话），
  严禁入库，只在可信范围内传输。
EOF
}

GROUP_IDS=()
OPENCLAW_HOME="$HOME/.openclaw"
OUT_DIR="$PWD"
INCLUDE_WORKSPACES="false"
REDACT="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --include-workspaces) INCLUDE_WORKSPACES="true"; shift ;;
    --no-redact) REDACT="false"; shift ;;
    --out)
      [[ $# -ge 2 ]] || { echo "ERROR: --out 需要一个目录" >&2; exit 1; }
      OUT_DIR="$2"; shift 2 ;;
    --openclaw-home)
      [[ $# -ge 2 ]] || { echo "ERROR: --openclaw-home 需要一个目录" >&2; exit 1; }
      OPENCLAW_HOME="$2"; shift 2 ;;
    --*) echo "ERROR: 未知选项: $1" >&2; usage >&2; exit 1 ;;
    *)
      # 群号只认纯数字，早点拦住拼错的参数（否则会安静地产出空包）
      [[ "$1" =~ ^[0-9]+$ ]] || { echo "ERROR: 群号必须是纯数字: $1" >&2; exit 1; }
      GROUP_IDS+=("$1"); shift ;;
  esac
done

if [[ ${#GROUP_IDS[@]} -eq 0 ]]; then
  echo "ERROR: 至少要给一个群号" >&2
  usage >&2
  exit 1
fi

OPENCLAW_HOME="${OPENCLAW_HOME/#\~/$HOME}"
OUT_DIR="${OUT_DIR/#\~/$HOME}"

[[ -d "$OPENCLAW_HOME" ]] || { echo "ERROR: openclaw 家目录不存在: $OPENCLAW_HOME" >&2; exit 1; }

# 优先用 openclaw 自带的 python（生产机上系统 python 可能太老或没有）。
# 和 inspector.py 的 openclaw_python() 同一个选择逻辑。
PY_BIN="$OPENCLAW_HOME/python/bin/python3"
[[ -x "$PY_BIN" ]] || PY_BIN="$(command -v python3 || true)"
[[ -n "$PY_BIN" ]] || { echo "ERROR: 找不到 python3（试过 $OPENCLAW_HOME/python/bin/python3 和 PATH）" >&2; exit 1; }

command -v zip >/dev/null 2>&1 || { echo "ERROR: 没有 zip 命令" >&2; exit 1; }

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
# 多个群时名字只取第一个 + 个数，避免文件名过长
if [[ ${#GROUP_IDS[@]} -eq 1 ]]; then
  TAG="${GROUP_IDS[0]}"
else
  TAG="${GROUP_IDS[0]}-等${#GROUP_IDS[@]}群"
fi
PACKAGE_NAME="openclaw-inspector-${TAG}-${TIMESTAMP}"
OUT_ZIP="$OUT_DIR/${PACKAGE_NAME}.zip"

STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/openclaw-inspector-collect.XXXXXX")"
cleanup() {
  if [[ -n "${STAGING_DIR:-}" && -d "$STAGING_DIR" \
        && "$STAGING_DIR" == "${TMPDIR:-/tmp}"/openclaw-inspector-collect.* ]]; then
    rm -rf "$STAGING_DIR"
  fi
}
trap cleanup EXIT
PACKAGE_DIR="$STAGING_DIR/$PACKAGE_NAME"
mkdir -p "$PACKAGE_DIR"

"$PY_BIN" - "$OPENCLAW_HOME" "$PACKAGE_DIR" "$INCLUDE_WORKSPACES" "$REDACT" "${GROUP_IDS[@]}" <<'PYEOF'
# -*- coding: utf-8 -*-
"""按群号采集巡检器回归素材。包内目录镜像 ~/.openclaw，解包即可当数据根用。"""
import datetime
import glob
import json
import os
import platform
import shutil
import sqlite3
import sys

openclaw_home = os.path.realpath(sys.argv[1])
package_dir = sys.argv[2]
include_workspaces = sys.argv[3] == "true"
redact = sys.argv[4] == "true"
group_ids = [str(g) for g in sys.argv[5:]]

# 包内数据根。镜像 ~/.openclaw 的相对结构，offline_replay.py 可直接指向它。
mirror = os.path.join(package_dir, "openclaw")

# 和 inspector.py 保持一致的三个常量。**必须逐字同步** —— 这里挑错了库/目录，
# 表现是回放时判据静默读不到数据，看起来像判据坏了。
AGENT_DB_RELPATH = os.path.join("agent", "openclaw-agent.sqlite")
GROUP_SEGMENTS = ("group", "group-virtual")
DELIVERY_WORKFLOW_PATTERNS = ("zqjz-*.json", "zqjz-*.yaml", "zqjz-*.yml")

warnings = []


def warn(msg):
    warnings.append(msg)
    print(f"  ⚠️  {msg}", file=sys.stderr)


def parse_group_id(session_key):
    """从 sessionKey 解出群号。**与 inspector.parse_group_id 逐字同构**。

    三种实际形态（群号都是紧跟 group 段之后的第一个纯数字段）：
      agent:zqjzszr:jingme:group:10232767188
      agent:cangjie:jingme:group-virtual:10232767188:zqjzszr
      agent:shenkuo:jingme:group:dw.zqjz.ts1:group:10232767188
    """
    parts = str(session_key).split(":")
    for i, seg in enumerate(parts):
        if seg in GROUP_SEGMENTS:
            for later in parts[i + 1:]:
                if later.isdigit():
                    return later
    return None


def mkdirp(path):
    os.makedirs(path, exist_ok=True)


def mirror_dest(src):
    """把 ~/.openclaw 下的绝对路径映射到包内镜像的同一相对位置。"""
    rel = os.path.relpath(os.path.realpath(src), openclaw_home)
    if rel.startswith(".."):
        return None          # 不在家目录里，不收（防止跟着符号链接跑出去）
    return os.path.join(mirror, rel)


def copy_file(src, dest):
    if not os.path.isfile(src):
        return False
    mkdirp(os.path.dirname(dest))
    shutil.copy2(src, dest)
    return True


DENY_NAMES = {".git", "node_modules", "dist", "build", ".cache", ".venv", "__pycache__"}


def copy_tree(src, dest):
    """拷目录，跳过构建产物和明显的凭据文件。返回拷了多少个文件。"""
    if not os.path.isdir(src):
        return 0
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in DENY_NAMES
                   and not os.path.islink(os.path.join(root, d))]
        for name in files:
            if name == ".env" or name.endswith((".pem", ".key")):
                continue
            sp = os.path.join(root, name)
            if os.path.islink(sp):
                continue
            if copy_file(sp, os.path.join(dest, os.path.relpath(sp, src))):
                n += 1
    return n


# ---------- openclaw.json 脱敏 ----------

SECRET_HINTS = ("token", "secret", "password", "passwd", "apikey", "api_key",
                "credential", "cookie", "privatekey")


def redact_json(obj, path=""):
    """把 token/密钥类字段的值换成 <REDACTED>，返回 (脱敏后对象, 打码字段列表)。

    ⚠️ `${ENV_VAR}` 这种占位符**不打码** —— 它本身不是密钥，而是"从哪个环境变量取"
    这条配置信息，打掉反而看不出配置形态。实测本机 openclaw.json 里
    memory.search.remote.apiKey 就是 `${SUPER_PERSON_JOYBUILDER_API_KEY}`，
    而 gateway.auth.token 是真实明文 —— 后者必须打掉。
    """
    hit = []

    def walk(node, prefix):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                kp = f"{prefix}.{k}" if prefix else k
                low = k.lower().replace("-", "").replace("_", "")
                if (any(h in low for h in SECRET_HINTS)
                        and isinstance(v, str) and v
                        and not (v.startswith("${") and v.endswith("}"))):
                    out[k] = "<REDACTED>"
                    hit.append(kp)
                else:
                    out[k] = walk(v, kp)
            return out
        if isinstance(node, list):
            return [walk(v, f"{prefix}[{i}]") for i, v in enumerate(node)]
        return node

    return walk(obj, path), hit


# ---------- SQLite 一致性快照 ----------

def backup_db(src, dest):
    """用 sqlite3 的 backup API 拷库，而不是 cp。

    ⚠️ 必须这么拷。理由两条，都踩过或差点踩：
      1. 生产库正被 openclaw 持续写入，`cp` 可能拿到撕裂的文件；backup API 走的是
         SQLite 自己的一致性读，拷出来必然是个完整库。
      2. backup 会把 -wal 里还没 checkpoint 的内容**一并落进目标库**。本机实测主库
         6.7MB、WAL 里还压着 2.3MB —— 只 cp 主库会丢掉最近几十分钟的事件，
         表现是"这个群怎么突然不动了"。落进单文件后目标包也不用带 -wal/-shm。
    只读打开、不用 immutable=1（那会假设文件不变，正被写入的库会读到过期快照）。
    """
    mkdirp(os.path.dirname(dest))
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        warn(f"打不开库 {src}: {exc}")
        return False
    try:
        dst_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
        return True
    except sqlite3.Error as exc:
        warn(f"拷库失败 {src}: {exc}")
        if os.path.exists(dest):
            os.remove(dest)
        return False
    finally:
        src_conn.close()


def db_session_keys(db_path):
    """读一个库里的 {session_key: (sessionId, status)}；读不了返回空。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT session_key, current_session_id, status FROM session_nodes").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows if isinstance(r[0], str) and r[0]}
    except sqlite3.Error as exc:
        # 没有 session_nodes 表 = 这个实例还是旧的 JSON 布局。这是个必须说出来的信号，
        # 不能静默跳过：巡检器对这类实例本来就失明，采集也拿不到东西。
        warn(f"{db_path} 读不到 session_nodes（{exc}）—— 这个 agent 可能还是 JSON 布局")
        return {}
    finally:
        conn.close()


def event_counts(db_path, session_id):
    """某条会话的 (transcript 条数, trajectory 条数)，用来在 manifest 里体现素材量。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error:
        return 0, 0
    out = []
    try:
        for table in ("transcript_events", "trajectory_runtime_events"):
            try:
                out.append(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",
                    (session_id,)).fetchone()[0])
            except sqlite3.Error:
                out.append(0)
    finally:
        conn.close()
    return tuple(out)


def read_meta_field(text, name):
    """从 META.md 取 `- **字段**: 值`。与 inspector.read_meta_field 同构。"""
    for line in text.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if not stripped.startswith("**"):
            continue
        head, _, tail = stripped.partition("**:")
        if not tail:
            head, _, tail = stripped.partition("**：")
        if head.strip("* ") == name:
            return tail.strip()
    return ""


def encode_cc_dir(path):
    """项目源码路径 → cc-config 下的目录名。与 inspector.encode_cc_dir 同构。"""
    return path.replace("/", "-").replace(".", "-")


# ---------- 主流程 ----------

manifest = {
    "schemaVersion": 2,
    "tool": "openclaw-inspector-collect",
    "collectedAt": datetime.datetime.now(datetime.timezone.utc)
                   .isoformat(timespec="seconds").replace("+00:00", "Z"),
    "groupIds": group_ids,
    "openclawHome": openclaw_home,
    "layout": "sqlite",
    "redacted": redact,
    "includeWorkspaces": include_workspaces,
    "host": {"platform": platform.system(), "release": platform.release(),
             "hostname": platform.node(), "user": os.environ.get("USER", "")},
    "digitalEmployees": [],
    "shared": {},
    "warnings": warnings,
}

# 实例判据和 inspector.discover_digital_humans 一致：目录里有 openclaw.json。
instances = []
for inst_dir in sorted(glob.glob(os.path.join(openclaw_home, "*"))):
    if os.path.isfile(os.path.join(inst_dir, "openclaw.json")):
        instances.append(inst_dir)

if not instances:
    warn(f"{openclaw_home} 下没有任何实例目录（判据：目录里有 openclaw.json）")

for inst_dir in instances:
    da_id = os.path.basename(inst_dir)
    print(f"\n数字人 {da_id}")

    # 先扫一遍所有 agent 的库，找出"参与了这些群"的 agent。
    # 只拷参与的 —— 一个实例下可能挂着十几个 agent，全拷会把包撑大好几倍。
    matched_agents = {}      # agentId -> {"db":路径, "sessions":[...]}
    for db_path in sorted(glob.glob(os.path.join(inst_dir, "agents", "*", AGENT_DB_RELPATH))):
        agent_id = db_path.split(os.sep)[-3]
        hits = []
        for session_key, (session_id, status) in db_session_keys(db_path).items():
            gid = parse_group_id(session_key)
            if gid not in group_ids:
                continue
            te, tr = event_counts(db_path, session_id)
            hits.append({"groupId": gid, "sessionKey": session_key,
                         "sessionId": session_id, "status": status,
                         "transcriptEvents": te, "trajectoryEvents": tr})
        if hits:
            matched_agents[agent_id] = {"db": db_path, "sessions": hits}

    if not matched_agents:
        print(f"  跳过：没有会话落在 {', '.join(group_ids)} 这些群里")
        continue

    entry = {"daId": da_id, "instDir": inst_dir, "agents": [],
             "workflows": [], "files": []}

    # 1. openclaw.json（实例判据）—— 脱敏后写进镜像
    src_cfg = os.path.join(inst_dir, "openclaw.json")
    dest_cfg = mirror_dest(src_cfg)
    if dest_cfg:
        try:
            with open(src_cfg, encoding="utf-8") as f:
                cfg = json.load(f)
            if redact:
                cfg, hit = redact_json(cfg)
                if hit:
                    print(f"  openclaw.json 已打码 {len(hit)} 个字段: {', '.join(hit)}")
                    entry["redactedFields"] = hit
            mkdirp(os.path.dirname(dest_cfg))
            with open(dest_cfg, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            entry["files"].append(os.path.relpath(dest_cfg, package_dir))
        except (OSError, ValueError) as exc:
            warn(f"处理 {src_cfg} 失败: {exc}")

    # 2. 各 agent 的库（一致性快照）
    for agent_id, info in sorted(matched_agents.items()):
        dest_db = mirror_dest(info["db"])
        if not dest_db:
            continue
        ok = backup_db(info["db"], dest_db)
        size = os.path.getsize(dest_db) if ok and os.path.exists(dest_db) else 0
        total_ev = sum(s["transcriptEvents"] + s["trajectoryEvents"] for s in info["sessions"])
        print(f"  库 {agent_id:12s} {size / 1024 / 1024:6.2f}MB  "
              f"{len(info['sessions'])} 条本群会话 / {total_ev} 条事件")
        entry["agents"].append({
            "agentId": agent_id, "role": "数字人" if agent_id == da_id else "基础Agent",
            "db": os.path.relpath(dest_db, package_dir) if ok else None,
            "dbBytes": size, "sessions": info["sessions"],
        })
        if ok:
            entry["files"].append(os.path.relpath(dest_db, package_dir))

    # 3. private-experts/<da>/workflow/（交付数字人判据）
    wf_src = os.path.join(inst_dir, "private-experts", da_id, "workflow")
    wf_dest = mirror_dest(wf_src)
    if wf_dest and os.path.isdir(wf_src):
        n = copy_tree(wf_src, wf_dest)
        names = set()
        for pattern in DELIVERY_WORKFLOW_PATTERNS:
            for p in glob.glob(os.path.join(wf_src, pattern)):
                names.add(os.path.basename(p))
        entry["workflows"] = sorted(names)
        # 交付数字人判据就是"这里有 zqjz-* 文件"。一个都没有的话回放时
        # is_delivery_agent 会判否、整个实例被跳过 —— 值得当场说出来。
        print(f"  workflow/ {n} 个文件，交付工作流 {len(names)} 个"
              f"{'  ⚠️ 一个都没有，回放时这个数字人会被判成非交付' if not names else ''}")
    else:
        warn(f"{da_id} 没有 private-experts/{da_id}/workflow/ —— 回放时会被判成非交付数字人")

    # 4. 发送脚本所在目录（只收脚本本身，回放不发消息但便于核对路由）
    send_src = os.path.join(inst_dir, "private-experts", da_id, "skills",
                            "zqjz-agent-runtime-specs", "scripts", "send-user-message.py")
    send_dest = mirror_dest(send_src)
    if send_dest and copy_file(send_src, send_dest):
        entry["files"].append(os.path.relpath(send_dest, package_dir))

    # 5. 可选：AGENTS.md / skills/（排查上下文，体积大）
    if include_workspaces:
        for rel in ("AGENTS.md", "skills"):
            src = os.path.join(inst_dir, "private-experts", da_id, rel)
            dest = mirror_dest(src)
            if not dest:
                continue
            if os.path.isdir(src):
                copy_tree(src, dest)
            else:
                copy_file(src, dest)

    manifest["digitalEmployees"].append(entry)

# ---------- 公共配置（不按数字人分，整个 openclaw 家目录一份） ----------

print("\n公共配置")
shared = manifest["shared"]

# 功能 24 的编排层。⚠️ 在 ~/.openclaw/ 根下，**不在**实例目录里 —— 这点很容易找错。
shared["workflowContexts"] = []
for gid in group_ids:
    src = os.path.join(openclaw_home, "contexts", "groups", gid)
    dest = mirror_dest(src)
    if dest and os.path.isdir(src):
        n = copy_tree(src, dest)
        states = len(glob.glob(os.path.join(src, "*", "workflow-instances", "*", "state.json")))
        shared["workflowContexts"].append({"groupId": gid, "files": n, "stateFiles": states})
        print(f"  contexts/groups/{gid}: {n} 个文件，{states} 个 state.json")
    else:
        print(f"  contexts/groups/{gid}: 没有（功能 24 在这个群上无素材）")

# 步骤 0.5 结项闸门：只收群号对得上的项目，其他项目的 META.md 不要。
shared["projects"] = []
for meta_path in sorted(glob.glob(os.path.join(openclaw_home, "projects", "*", "META.md"))):
    try:
        with open(meta_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        continue
    gid = read_meta_field(text, "群聊ID")
    if gid not in group_ids:
        continue
    dest = mirror_dest(meta_path)
    if not dest or not copy_file(meta_path, dest):
        continue
    project_dir = os.path.dirname(meta_path)
    project_key = os.path.basename(project_dir)
    rec = {"projectKey": project_key, "groupId": gid,
           "metaStatus": read_meta_field(text, "状态"),
           "closeStatus": read_meta_field(text, "finalDeliveryCloseStatus"),
           "closedAt": read_meta_field(text, "结项时间")}
    # 信号 4 的 CC session.json（判据当前禁用，但素材顺手收着）
    cc_dir = os.path.join(openclaw_home, "cc-config", "projects",
                          encode_cc_dir(os.path.join(project_dir, "src")))
    # 整个 cc-config 项目目录都收（不只 session.json）——「这个项目有没有 CC 目录」
    # 本身就是个判据（方案 §8.2 E：没目录则降级为不巡检），只拷一个文件的话
    # "目录存在但还没建 session.json" 和 "目录压根不存在" 在包里分不出来。
    cc_dest_dir = mirror_dest(cc_dir)
    if cc_dest_dir and os.path.isdir(cc_dir):
        n_cc = copy_tree(cc_dir, cc_dest_dir)
        rec["ccDir"] = os.path.relpath(cc_dest_dir, package_dir)
        rec["ccFiles"] = n_cc
    cc_src = os.path.join(cc_dir, "session.json")
    cc_dest = mirror_dest(cc_src)
    if cc_dest and os.path.isfile(cc_dest):
        rec["ccSession"] = os.path.relpath(cc_dest, package_dir)
    shared["projects"].append(rec)
    print(f"  projects/{project_key}: 状态={rec['metaStatus'] or '(空)'} "
          f"结项={rec['closeStatus'] or '(空)'}")

if not shared["projects"]:
    print("  projects/: 这些群没有关联的 CC 项目（结项闸门在回放里不会生效）")

# ---------- 写 manifest 和说明 ----------

mkdirp(package_dir)
with open(os.path.join(package_dir, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

total_agents = sum(len(d["agents"]) for d in manifest["digitalEmployees"])
total_sessions = sum(len(a["sessions"]) for d in manifest["digitalEmployees"] for a in d["agents"])
total_events = sum(s["transcriptEvents"] + s["trajectoryEvents"]
                   for d in manifest["digitalEmployees"] for a in d["agents"]
                   for s in a["sessions"])

with open(os.path.join(package_dir, "README.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join([
        "巡检器回归素材包（openclaw-inspector-collect）",
        "",
        f"采集时间   {manifest['collectedAt']}",
        f"群号       {', '.join(group_ids)}",
        f"数字人     {len(manifest['digitalEmployees'])} 个",
        f"agent 库   {total_agents} 个",
        f"本群会话   {total_sessions} 条 / {total_events} 条事件",
        f"脱敏       {'是（token/密钥类字段已打码）' if redact else '否 —— 含明文凭据！'}",
        f"告警       {len(warnings)} 条（见 manifest.json 的 warnings）",
        "",
        "怎么用：",
        "  包内 openclaw/ 逐层镜像 ~/.openclaw，可直接当数据根：",
        f"    python3 offline_replay.py <本目录>/openclaw {group_ids[0]} --da <数字人>",
        "",
        "⚠️ 数据边界：",
        "  SQLite 布局下一个 agent 的所有群会话都在同一个库里，没法按群裁剪，",
        "  所以库里**含其他群的会话内容**。本包含真实群聊记录、工具输入输出、",
        "  业务上下文和文件路径 —— 严禁入库，只在可信范围内传输。",
        "",
    ]))

print(f"\n汇总：{len(manifest['digitalEmployees'])} 个数字人 / {total_agents} 个库 / "
      f"{total_sessions} 条本群会话 / {total_events} 条事件 / {len(warnings)} 条告警")
if not total_sessions:
    print("\n⚠️  没有任何会话落在指定的群里 —— 包是空的。", file=sys.stderr)
    print("    先确认：① 群号对不对 ② 这台机器上的实例是否已迁到 SQLite", file=sys.stderr)
    print(f"    ② 的确认方法：sqlite3 <实例>/agents/<da>/{AGENT_DB_RELPATH} "
          '"select count(*) from session_nodes;"', file=sys.stderr)
    sys.exit(2)
PYEOF
PY_STATUS=$?

if [[ $PY_STATUS -ne 0 ]]; then
  echo "" >&2
  echo "采集未产出有效素材（退出码 $PY_STATUS），不打包。" >&2
  exit $PY_STATUS
fi

( cd "$STAGING_DIR" && zip -qr "$OUT_ZIP" "$PACKAGE_NAME" )

echo ""
echo "素材包已生成：$OUT_ZIP"
echo "  大小 $(du -h "$OUT_ZIP" | cut -f1)"
echo ""
echo "拿回本机后："
echo "  unzip -q $(basename "$OUT_ZIP") -d ~/.openclaw-inspector/"
echo "  python3 offline_replay.py ~/.openclaw-inspector/${PACKAGE_NAME}/openclaw ${GROUP_IDS[0]} --da <数字人>"
echo ""
echo "⚠️  包内含真实群聊内容（整库拷贝，含库内其他群会话），严禁入库。"
