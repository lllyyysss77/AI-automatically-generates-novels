#!/usr/bin/env bash
# 长跑守护：写满目标章节为止，崩了自动续跑（断点在 state.json 里）。
#   bash scripts/run_until.sh "书名" [目标章数] [每轮章数]
set -uo pipefail
cd "$(dirname "$0")/.."
TITLE="${1:?书名}"; TARGET="${2:-0}"; BATCH="${3:-20}"
SLUG=$(python3 -c "import sys;sys.path.insert(0,'.');from server.orchestrator import slugify;print(slugify('$TITLE'))")
LOG="reports/longrun.log"; FAILS=0

count() { python3 -c "
import json;print(len(json.load(open('projects/$SLUG/state.json',encoding='utf-8')).get('done',[])))" 2>/dev/null || echo 0; }
target() { python3 -c "
import json;print(json.load(open('projects/$SLUG/project.json',encoding='utf-8')).get('target_chapters',0))" 2>/dev/null || echo 0; }
[ "$TARGET" -eq 0 ] && TARGET=$(target)

echo "=== 长跑启动 $(date '+%F %T')  目标 $TARGET 章，当前 $(count) 章 ===" | tee -a "$LOG"
while [ "$(count)" -lt "$TARGET" ]; do
  BEFORE=$(count)
  python3 -u run_novel.py run --title "$TITLE" --chapters "$BATCH" >> "$LOG" 2>&1
  AFTER=$(count)
  if [ "$AFTER" -le "$BEFORE" ]; then
    FAILS=$((FAILS+1))
    echo "!! 本轮无进展（$BEFORE -> $AFTER），第 $FAILS 次；60s 后重试" | tee -a "$LOG"
    [ "$FAILS" -ge 5 ] && { echo "!! 连续 5 轮无进展，停止" | tee -a "$LOG"; exit 1; }
    sleep 60
  else
    FAILS=0
    echo "-- 进度 $AFTER/$TARGET  $(date '+%T')" | tee -a "$LOG"
  fi
done
echo "=== 写完 $(count) 章 $(date '+%F %T') ===" | tee -a "$LOG"
python3 run_novel.py status --title "$TITLE" | tee -a "$LOG"
