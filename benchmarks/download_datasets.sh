#!/usr/bin/env bash
# Phase 5: Benchmark 数据集下载脚本
# 在 Linux 环境（Docker/VM）上执行此脚本下载公共 NL2SQL 基准数据集
#
# Usage: bash benchmarks/download_datasets.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATASETS_DIR="${SCRIPT_DIR}/datasets"

echo "=== Phase 5: 下载 NL2SQL Benchmark 数据集 ==="
echo "目标目录: ${DATASETS_DIR}"

# ── 1. BIRD Benchmark (dev set) ──────────────────────────────────────────────
BIRD_DIR="${DATASETS_DIR}/bird"
mkdir -p "${BIRD_DIR}"

if [ ! -f "${BIRD_DIR}/dev.json" ]; then
    echo ""
    echo "[1/3] 下载 BIRD dev set..."
    # 官方 OSS 源
    wget -q --show-progress -O "${BIRD_DIR}/dev.zip" \
        "https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip" || \
    # 备选: GitHub 镜像
    wget -q --show-progress -O "${BIRD_DIR}/dev.zip" \
        "https://github.com/AlibabaResearch/DAMO-ConvAI/raw/main/bird/data/dev.zip"

    echo "  解压..."
    cd "${BIRD_DIR}"
    unzip -qo dev.zip
    # BIRD dev.zip 解压后通常有 dev/dev.json
    if [ -f "dev/dev.json" ]; then
        mv dev/dev.json .
        mv dev/dev_databases dev_databases 2>/dev/null || true
        rm -rf dev
    fi
    rm -f dev.zip
    echo "  BIRD dev set 就绪: $(wc -l < dev.json 2>/dev/null || echo '?') 行"
else
    echo "[1/3] BIRD dev set 已存在，跳过"
fi

# ── 2. Spider 1.0 (dev set) ─────────────────────────────────────────────────
SPIDER_DIR="${DATASETS_DIR}/spider"
mkdir -p "${SPIDER_DIR}"

if [ ! -f "${SPIDER_DIR}/dev.json" ]; then
    echo ""
    echo "[2/3] 下载 Spider 1.0 dev set..."
    wget -q --show-progress -O "${SPIDER_DIR}/spider.zip" \
        "https://drive.google.com/uc?id=1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J&export=download&confirm=t" || \
    # 备选: Yale 原始源
    wget -q --show-progress -O "${SPIDER_DIR}/spider.zip" \
        "https://yale-lily.github.io/spider/spider.zip" || \
    echo "  [WARN] Spider 自动下载失败，请手动下载:"
    echo "    1. 访问 https://yale-lily.github.io/spider"
    echo "    2. 下载 spider.zip"
    echo "    3. 解压 dev.json 到 ${SPIDER_DIR}/"

    if [ -f "${SPIDER_DIR}/spider.zip" ]; then
        echo "  解压..."
        cd "${SPIDER_DIR}"
        unzip -qo spider.zip
        # Spider zip 解压后通常有 spider/dev.json
        if [ -f "spider/dev.json" ]; then
            mv spider/dev.json .
            mv spider/tables.json . 2>/dev/null || true
            rm -rf spider
        fi
        rm -f spider.zip
        echo "  Spider dev set 就绪: $(wc -l < dev.json 2>/dev/null || echo '?') 行"
    fi
else
    echo "[2/3] Spider dev set 已存在，跳过"
fi

# ── 3. 验证 ─────────────────────────────────────────────────────────────────
echo ""
echo "=== 数据集状态 ==="
for ds in bird spider enterprise; do
    dir="${DATASETS_DIR}/${ds}"
    if [ -d "${dir}" ]; then
        count=$(find "${dir}" -name "*.json" -o -name "*.jsonl" | wc -l)
        echo "  ${ds}: ${count} 个 JSON 文件"
    else
        echo "  ${ds}: 未找到"
    fi
done

echo ""
echo "=== 完成 ==="
echo "如需手动下载，请参考:"
echo "  BIRD:   https://bird-bench.github.io/"
echo "  Spider: https://yale-lily.github.io/spider"
echo "  DuSQL:  https://aistudio.baidu.com/competition/detail/47"
