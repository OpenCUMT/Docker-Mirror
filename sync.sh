#!/bin/bash

# ==============================================
# Docker 镜像同步脚本 (使用 skopeo 保留多平台)
# 功能：从配置文件读取源镜像，添加前缀后推送到目标仓库
# 每次运行覆盖旧日志，执行中追加
# ==============================================

# ---------- 日志文件路径 ----------
FULL_LOG="./sync_full.log"
ERROR_LOG="./sync_error.log"

> "$FULL_LOG"
> "$ERROR_LOG"

# ---------- 日志函数 ----------
log() {
    echo "$@" | tee -a "$FULL_LOG"
}

log_error() {
    local src="$1"
    local dest="$2"
    local reason="$3"
    echo "[ERROR] $src → $dest : $reason" | tee -a "$FULL_LOG" "$ERROR_LOG"
}


# ---------- 1. 目标仓库前缀 ----------
PREFIX="swr.ap-southeast-1.myhuaweicloud.com/opencumt"

# ---------- 2. 配置文件路径 ----------
CONFIG_FILE="./images.conf"

if [ ! -f "$CONFIG_FILE" ]; then
    log "❌ 配置文件 $CONFIG_FILE 不存在"
    exit 1
fi

log "📄 读取配置文件：$CONFIG_FILE"
log "🏷️  目标前缀：$PREFIX"

STATE_FILE="./sync.state"

LAST_SYNC=""

if [ -f "$STATE_FILE" ]; then
    LAST_SYNC=$(cat "$STATE_FILE")
    log "📌 从断点恢复：$LAST_SYNC"
fi

FOUND_START=true

# 如果存在断点，需要先找到它
if [ -n "$LAST_SYNC" ]; then
    FOUND_START=false
fi

# ---------- 3. 同步镜像 ----------
while IFS= read -r line || [ -n "$line" ]; do
    # 去除首尾空格
    line=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

    # 跳过空行与注释
    [ -z "$line" ] && continue
    [[ "$line" =~ ^# ]] && continue

    src="$line"
    
    # 还没有到恢复位置
	if [ "$FOUND_START" = false ]; then
	    if [ "$src" = "$LAST_SYNC" ]; then
	        FOUND_START=true
	    fi
	    continue
	fi
    # 目标地址拼接（注意添加 docker:// 协议前缀）
    dest="${PREFIX}/${src}"

    log "----------------------------------------"
    log "🔄 同步: docker://${src} → docker://${dest}"

    # --all：复制所有平台，保留 manifest list
    OUTPUT=$(skopeo copy --all \
    "docker://${src}" \
    "docker://${dest}" 2>&1)
	RET=$?
	if [ $RET -eq 0 ]; then
	    log "$OUTPUT"
	    log "✅ 完成: $dest"
	    # 更新断点
	    echo "$src" > "$STATE_FILE"
	else
	    log "$OUTPUT"
	    if echo "$OUTPUT" | grep -Eiq "toomanyrequests|pull rate limit"; then
	        log "🚫 Docker Hub 拉取额度耗尽"
	        log "💾 已保存断点，下次继续"
	        exit 0
	    fi
	    log "❌ 同步失败: $dest"
	    log_error "$src" "$dest" "sync failed"
	fi
done < "$CONFIG_FILE"

log "========================================"
rm -f "$STATE_FILE"
log "🎉 全部任务处理完毕"