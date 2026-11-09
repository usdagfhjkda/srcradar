# syntax=docker/dockerfile:1.7
# ============================================================================
# srcradar — multi-stage Docker build (the full component set)
#
# One Dockerfile builds every variant; the build args below select the
# language, the DNS resolver set and the modules that differ.
#
# Stage 1 (builder): golang:1.25 image that builds the PD toolchain +
#                    cdnmatch + the algo_diff_cpp core (C++) +
#                    db_align + the ENScan_GO mirror.
# Stage 2 (runtime): ubuntu:22.04 slim with bash + python3 + sqlite3 + cron,
#                    receives only the binaries + srcradar source.
#
# Image tags:
#   srcradar:1.0.0_zh   — the Chinese build of the 1.0.0 release
#
# Build:
#   # the Chinese variant
#   docker build -t srcradar:1.0.0_zh \
#       --build-arg SRCRADAR_VERSION=1.0.0 \
#       --build-arg SRCDAR_LANG=zh \
#       --build-arg SRCDAR_RESOLVER=cn \
#       --build-arg SRCDAR_WITH_DBALIGN=1 \
#       --build-arg SRCDAR_WITH_YMICP=1 .
#
#   # the English variant
#   docker build -t srcradar:1.0.0 \
#       --build-arg SRCRADAR_VERSION=1.0.0 \
#       --build-arg SRCDAR_LANG=en \
#       --build-arg SRCDAR_RESOLVER=global \
#       --build-arg SRCDAR_WITH_DBALIGN=0 \
#       --build-arg SRCDAR_WITH_YMICP=0 .
#
# The variant is selected by the build args below, so one Dockerfile builds
# every variant:
#   SRCDAR_LANG          en | zh
#   SRCDAR_RESOLVER      global | cn
#   SRCDAR_WITH_DBALIGN  1 = the db_align module and its ENScan mirror
#   SRCDAR_WITH_YMICP    1 = the ymicp client (its server is not shipped)
#
# The image is published as a tarball:
#   docker save -o srcradar-1.0.0_zh.tar srcradar:1.0.0_zh
#
# Component set (the full set, minus the excluded ones):
#   included: modules/main/{pdtm,daily,db,manage,i18n,lib}
#             modules/public/{katana,health,notify,db_align,ymicp}
#             srcradar-algo-diff-cpp (standalone repo, built from source)
#             srcradar-mcp-server     (standalone repo, python daemon)
#   excluded: modules/private/*        (and the private tree it belongs to)
#
# The ymicp module holds the client only. Its server is the third party
# image yiminger/ymicp, which carries no open source license: it is not
# redistributed here, the operator deploys it on its own
# (modules/public/ymicp/docker-compose.yml is the reference compose file).
# The client reads its base URL from YMICP_BASE (default 127.0.0.1:16181),
# so it has to point at wherever the operator deployed the server.
#
# Runtime defaults (see docker/entrypoint.sh):
#   SRCDAR_LANG=zh          the language of every user-visible output
#   resolver_set=cn         the DNS resolver set of pdtm.conf
#   dashboard   0.0.0.0:8765 (mapped to 127.0.0.1:8765 on the host)
#   mcp-server  0.0.0.0:8764 (mapped to 127.0.0.1:8764 on the host)
#
# Q1-Q4 defaults (TODO markers; CHANGELOG next bump may revise):
#   Q1: PD tool version — pinned via PD_VERSION ARG below; bump manually
#   Q2: algo_diff_cpp vendor — the pins live in its own install.sh
#   Q3: ENScan cookie   — the operator provides it (a mounted file or a
#                         mounted /data/enscan directory); the enscan stage
#                         fails without it, the other stages do not
#   Q4: cron            — entrypoint starts service cron; daily 03:00 CST
# ============================================================================

# ----------------------------------------------------------------------------
# The variant of the image. Declared before the first FROM, so the builder and
# the runtime share ONE value: a stage level ARG would otherwise default to a
# different value in each stage (the builder would skip the build while the
# runtime still tried to copy the result).
# ----------------------------------------------------------------------------
ARG SRCDAR_LANG=zh
ARG SRCDAR_RESOLVER=cn
ARG SRCDAR_WITH_DBALIGN=1
ARG SRCDAR_WITH_YMICP=1

# ----------------------------------------------------------------------------
# Stage 1: builder
# ----------------------------------------------------------------------------
FROM golang:1.25 AS builder

# import the variant args into this stage
ARG SRCDAR_LANG
ARG SRCDAR_RESOLVER
ARG SRCDAR_WITH_DBALIGN
ARG SRCDAR_WITH_YMICP

# 锁的是 srcradar release tag -> image 内 pdtm 版本(image 本身不可变即锁)
ARG PDTM_VERSION=v0.1.5
ARG FFUF_VERSION=v2.1.0
ARG GAU_VERSION=v2.2.3
# pingc0y/URLFinder(Linux x86_64 binary release);与 PD urlfinder 不同
ARG URLFINDER_VERSION=2026.6.16
# algo_diff_cpp 的 vendor pin 在它自己的 install.sh 里,这里只锁 repo
ARG ALGO_DIFF_CPP_REPO=https://github.com/usdagfhjkda/srcradar-algo-diff-cpp.git

ENV GOPROXY=https://proxy.golang.org,direct \
    CGO_ENABLED=0 \
    GOFLAGS=-trimpath

WORKDIR /build

# ---- 1) pdtm 包管理器(pinned v0.1.5)----
# 只装 srcradar 实际需要的 6 个 PD 工具(去掉 nuclei/cloudlist/katana 等不用工具,
# 这些工具即使装了 srcradar 也不调用;pdtm -ia 会拉 ~25 个共 ~600MB).
RUN go install "github.com/projectdiscovery/pdtm/cmd/pdtm@${PDTM_VERSION}" && \
    pdtm -duc -i dnsx,httpx,subfinder,alterx,naabu,cdncheck

# ---- 1.5) ffuf + gau(非 PD 工具链,pdtm -ia 不管)----
# ffuf: joona-h/ffuf;gau: lc/gau
# 这两个都是 Go module,直接 go install 装到 GOPATH/bin
RUN go install "github.com/ffuf/ffuf/v2@${FFUF_VERSION}" && \
    go install "github.com/lc/gau/v2/cmd/gau@${GAU_VERSION}"

# ---- 1.6) pingc0y/URLFinder(Linux x86_64 binary release)----
# 不是 Go module,是 GitHub release binary;直接 curl 下载解压
# 与 PD urlfinder 不同:pingc0y 中文社区版,扫描策略不同
ARG URLFINDER_ARCH=Linux_x86_64
# /out-bin 由 step 5 创建,这里手动 mkdir 否则 mv 失败
RUN mkdir -p /out-bin /tmp/urlfinder && \
    curl -fsSL "https://github.com/pingc0y/URLFinder/releases/download/${URLFINDER_VERSION}/URLFinder_${URLFINDER_ARCH}.tar.gz" \
        -o /tmp/urlfinder/URLFinder.tar.gz && \
    tar -xzf /tmp/urlfinder/URLFinder.tar.gz -C /tmp/urlfinder && \
    mv /tmp/urlfinder/URLFinder /out-bin/URLFinder && \
    chmod +x /out-bin/URLFinder && \
    rm -rf /tmp/urlfinder

# ---- 2) cdnmatch(本仓 Go module,需要 cdncheck vendor)----
# 临时 clone cdncheck 当 vendor(只 builder 用,不入 runtime)
#
# cdncheck 的 tag 必须钉住:它的 master 已经要求 go >= 1.26.10,
# 而这个 builder 是 golang:1.25(go 1.25.14) 且 GOTOOLCHAIN=local
# (不自动下载新工具链),于是 go mod tidy 直接报
#   "module /tmp/cdncheck requires go >= 1.26.10"
# v1.3.0 是最后一个要求 go 1.25.0 的 tag,与本仓 in-tree vendor 一致。
# 升 golang 基线时要一起复核这个 pin。
ARG CDNCHECK_VERSION=v1.3.0
COPY modules/main/pdtm/cdnmatch /build/cdnmatch-src
RUN git clone --depth 1 --branch "${CDNCHECK_VERSION}" \
        https://github.com/projectdiscovery/cdncheck.git /tmp/cdncheck
WORKDIR /build/cdnmatch-src
RUN go mod edit -replace github.com/projectdiscovery/cdncheck=/tmp/cdncheck && \
    go mod tidy && \
    go build -o /out/cdnmatch . && \
    rm -rf /tmp/cdncheck

# ---- 3) massdns (GPL-3.0,shuffledns 的硬依赖)----
# massdns 是 C 写的,需要 gcc + make;从 GitHub 官方仓库 shallow clone
RUN git clone --depth 1 https://github.com/blechschmidt/massdns.git /tmp/massdns && \
    cd /tmp/massdns && make && \
    cp bin/massdns /out-bin/massdns && \
    chmod +x /out-bin/massdns && \
    rm -rf /tmp/massdns

# ---- 4) algo_diff_cpp(C++ 核心,独立仓)----
# 它是 srcradar 的独立仓,主仓的 modules/public/ 下没有它的目录。
# 构建需要 g++/cmake/make + libsqlite3-dev/zlib1g-dev,还要拉两个 vendor
# (rapidfuzz v3.3.2 / simdjson v3.9.5,pin 在它自己的 install.sh 里)。
# 编译产物是 bin/algo_diff_cpp,runtime 直接用这个二进制。
# libsimdjson-dev 是 algo_diff_cpp 的 check_deps 检查的包,但它自己的
# apt_install 列表里没有(上游脚本的疏漏),所以这里预先装好,
# 免得 install.sh 在 check_deps 上失败。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake libsqlite3-dev zlib1g-dev libsimdjson-dev \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 "${ALGO_DIFF_CPP_REPO}" /tmp/algo_diff_cpp \
    && cd /tmp/algo_diff_cpp \
    && bash install.sh --yes \
    && cp bin/algo_diff_cpp /out-bin/algo_diff_cpp \
    && chmod +x /out-bin/algo_diff_cpp \
    && rm -rf /tmp/algo_diff_cpp

# ---- 4.5) db_align(Go orchestrator; modernc.org/sqlite 纯 Go,无 CGO)----
# The module of the tree, built from its own source. The binary lands at
# modules/public/db_align/bin/db_align: that is the path run_one_business.sh
# calls ("$RECON_ROOT/public/db_align/bin/db_align").
# SRCDAR_WITH_DBALIGN=0 leaves it out (the English build).
COPY modules/public/db_align /build/db_align-src
WORKDIR /build/db_align-src
RUN if [ "${SRCDAR_WITH_DBALIGN}" = "1" ]; then \
        go build -o /out/db_align ./cmd/run; \
    else \
        echo "db_align is not part of this variant"; \
    fi

# ---- 4.6) ENScan(cloned from the archival mirror,Apache-2.0)----
# Source: usdagfhjkda/wgpsec-ENScan_GO, an archival mirror of the
# deleted upstream wgpsec/ENScan_GO. We pin the tag wgpsec-v1.4.0-fork1
# instead of a branch or SHA so that future upstream-archive revisions
# require an explicit Dockerfile bump, mirroring how PDTM_VERSION is pinned.
#
# The mirror carries the srcradar-specific changes (the qimai module and
# the interface fix), so it is used as it is; the modifications are
# documented in docker/MODIFICATIONS.txt.
#
# The binary lands at modules/public/ENScan_GO/ENScan: that is the path
# run_one_business.sh passes to db_align ("-enscan $RECON_ROOT/ENScan_GO/ENScan").
ARG ENSCAN_GO_REPO=https://github.com/usdagfhjkda/wgpsec-ENScan_GO.git
ARG ENSCAN_GO_TAG=wgpsec-v1.4.0-fork1
RUN if [ "${SRCDAR_WITH_DBALIGN}" = "1" ]; then \
        git clone --branch "${ENSCAN_GO_TAG}" "${ENSCAN_GO_REPO}" /tmp/ENScan_GO && \
        cd /tmp/ENScan_GO && \
        go build -o /out/ENScan . && \
        # the Apache-2.0 license of the mirror has to travel with the binary
        mkdir -p /out-license && \
        cp LICENSE /out-license/ENScan_GO-LICENSE && \
        rm -rf /tmp/ENScan_GO; \
    else \
        echo "ENScan is not part of this variant"; \
    fi

# ---- 5) 收集 builder 产物(pdtm 拉的 PD 工具 + 自建 binary)----
# pdtm -ia 把所有 PD 工具装到 $(go env GOPATH)/bin 或 ~/.pdtm/go/bin
# 兼容两种路径:go install 默认到 /go/bin;pdtm -ia 默认到 /root/.pdtm/go/bin
RUN mkdir -p /out /out-bin /out-license && \
    cp /out/cdnmatch /out-bin/ && \
    # pdtm 装的 PD 工具 — 兼容 GOPATH/bin 和 ~/.pdtm/go/bin
    for d in /go/bin /root/go/bin /root/.pdtm/go/bin; do \
        if [ -d "$d" ]; then \
            echo "Copying from $d:"; ls "$d"; \
            cp -f "$d"/* /out-bin/ 2>/dev/null || true; \
        fi; \
    done && \
    rm -f /out-bin/pdtm /out-bin/go && \
    echo "=== /out-bin contents ===" && ls -la /out-bin/ && \
    echo "=== /out-bin count ===" && ls /out-bin/ | wc -l

# ----------------------------------------------------------------------------
# Stage 2: runtime
# ----------------------------------------------------------------------------
FROM ubuntu:22.04 AS runtime

ARG SRCRADAR_VERSION=1.0.0
# import the variant args into this stage (they are declared before the first
# FROM, so both stages see the same value)
ARG SRCDAR_LANG
ARG SRCDAR_RESOLVER
ARG SRCDAR_WITH_DBALIGN
ARG SRCDAR_WITH_YMICP
# mcp-server 是独立仓,构建期 clone 进镜像。
# 这个 ARG 必须声明在 runtime 阶段:一个 ARG 只对它自己所在的 stage
# 和该 stage 的子 stage 可见,声明在 builder 里的 ARG 在 runtime 的
# RUN 里展开成空串(git 会报 "repository '' does not exist")。
ARG MCP_SERVER_REPO=https://github.com/usdagfhjkda/srcradar-mcp-server.git
ENV SRCDAR_VERSION=${SRCRADAR_VERSION} \
    TZ=Asia/Shanghai \
    SRCDAR_LANG=${SRCDAR_LANG} \
    PATH=/opt/srcradar/bin:/opt/srcradar:/usr/local/bin:/usr/bin:/bin

# ---- 1) 系统包 + 时区 ----
# 注意:srcradar 实际最低 py3.10 (pyproject.toml: target-version = "py310",
#   init.sh: PY_MIN_VERSION="3.10");ubuntu:22.04 默认 python3=3.10.6,
#   直接 apt install python3 即可。README 写的 ">= 3.12" 与代码层不一致,
#   是文档漂移,不是真依赖。
ARG DEBIAN_FRONTEND=noninteractive
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    apt-get update && apt-get install -y --no-install-recommends \
        bash \
        sqlite3 \
        util-linux \
        cron \
        ca-certificates \
        tini \
        tzdata \
        python3 \
        python3-requests \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/* && \
    mkdir -p /opt/srcradar/{bin,modules,logs,reports,snapshots,scan_results,config} && \
    mkdir -p /data && \
    chmod 755 /opt/srcradar /data

# ---- 2) 拷贝 builder 产物(pdtm -ia 拉的 PD 工具 + 自建 binary)----
# 遍历 /out-bin 全部拷贝;不再硬编码文件名,适应 pdtm 升级时新增工具
COPY --from=builder /out-bin/ /opt/srcradar/bin/

# ---- 3) 拷贝 srcradar 源码 ----
COPY srcradar          /opt/srcradar/srcradar
COPY init.sh           /opt/srcradar/init.sh
COPY install.sh        /opt/srcradar/install.sh
COPY check.sh          /opt/srcradar/check.sh
COPY pyproject.toml    /opt/srcradar/pyproject.toml
COPY LICENSE           /opt/srcradar/LICENSE
COPY NOTICE            /opt/srcradar/NOTICE
COPY TERMS_ADDENDUM.md /opt/srcradar/TERMS_ADDENDUM.md
COPY TERMS_ADDENDUM_zh.md /opt/srcradar/TERMS_ADDENDUM_zh.md
COPY VERSION           /opt/srcradar/VERSION
COPY modules           /opt/srcradar/modules

# ---- 3.1) 排除的模块不进镜像 ----
# private 目录本来就被 .dockerignore 排除(config/* 里的 modules/private/*),
# 这里再删一次是双保险(它只有 .gitkeep)。
RUN rm -rf /opt/srcradar/modules/private \
    && if [ "${SRCDAR_WITH_YMICP}" != "1" ]; then \
        # the module is not part of this variant: it must not be in the image
        rm -rf /opt/srcradar/modules/public/ymicp && \
        echo "ymicp is not part of this variant"; \
    fi \
    && echo "=== the modules in the image ===" && ls /opt/srcradar/modules/main /opt/srcradar/modules/public
# (db_align is removed by the step 3.1.2 when the variant does not carry it;
#  the private tree never ships.)

# ---- 3.1.1) the runtime leftovers of the pdtm module ----
# The pdtm module writes its runtime files (alive.txt / alterx.txt /
# dnsx_output.txt) into its own directory and refuses to run when it finds
# them there: they are the leftovers of a previous run, and the scanner
# would take the subdomains of another business as its input.
# The build context is filtered by .dockerignore, but the tree can still
# hold such a file, so they are removed from the image as well.
RUN rm -f /opt/srcradar/modules/main/pdtm/alive.txt \
          /opt/srcradar/modules/main/pdtm/alterx.txt \
          /opt/srcradar/modules/main/pdtm/dnsx_output.txt \
          /opt/srcradar/modules/main/pdtm/target.txt \
          /opt/srcradar/modules/main/pdtm/exclude.txt \
          /opt/srcradar/modules/main/pdtm/wildcard.txt \
          /opt/srcradar/modules/main/pdtm/no_wildcard.txt \
    && rm -rf /opt/srcradar/modules/main/pdtm/scan_results \
               /opt/srcradar/modules/main/pdtm/scan_outputs \
    && echo "=== the runtime leftovers of pdtm are gone ===" \
    && ls /opt/srcradar/modules/main/pdtm/ | grep -E '^(alive|alterx|dnsx_output|target|exclude|wildcard|no_wildcard)\.txt$' || echo "  (none left)"

# ---- 3.1.2) db_align 与 ENScan: 放到代码期望的路径 ----
# run_one_business.sh 调的是:
#   "$RECON_ROOT/public/db_align/bin/db_align" -n "$BIZ" -all -scope -delay 2 \
#       -db "$DB" -enscan "$RECON_ROOT/ENScan_GO/ENScan"
# 所以两个二进制必须落在这两个位置(不是 /opt/srcradar/bin/)。
# /out 只有 /out-bin 被拷进 runtime,所以这两个要单独拷。
# The runtime stage has no /out: only the builder has it. A conditional COPY
# is not possible (the file is absent in the variant that does not build it),
# so the builder output is mounted read only and the step below picks what
# the variant wants. The mount leaves no layer behind.
RUN --mount=from=builder,source=/out,target=/out,ro \
    if [ "${SRCDAR_WITH_DBALIGN}" = "1" ]; then \
        mkdir -p /opt/srcradar/modules/public/db_align/bin \
                     /opt/srcradar/modules/public/ENScan_GO && \
        cp /out/db_align /opt/srcradar/modules/public/db_align/bin/db_align && \
        cp /out/ENScan  /opt/srcradar/modules/public/ENScan_GO/ENScan && \
        chmod +x /opt/srcradar/modules/public/db_align/bin/db_align \
                 /opt/srcradar/modules/public/ENScan_GO/ENScan && \
        echo "=== db_align and ENScan ===" && \
        ls -l /opt/srcradar/modules/public/db_align/bin/ /opt/srcradar/modules/public/ENScan_GO/; \
    else \
        # the module is not part of this variant: it must not be in the image
        rm -rf /opt/srcradar/modules/public/db_align \
                 /opt/srcradar/modules/public/ENScan_GO && \
        echo "db_align is not part of this variant"; \
    fi

# ---- 3.2) algo_diff_cpp: 独立仓,主仓没有它的源码目录 ----
# daily_monitor.sh 按固定路径找它:
#   ALGO_DIFF_CPP_BIN="$RECON_ROOT/public/algo_diff_cpp/bin/algo_diff_cpp"
# 所以二进制必须落在 modules/public/algo_diff_cpp/bin/ 下,名字也不能改。
# /opt/srcradar/bin/algo_diff_cpp 是同一份文件的副本,方便直接调用。
RUN mkdir -p /opt/srcradar/modules/public/algo_diff_cpp/bin \
    && cp /opt/srcradar/bin/algo_diff_cpp /opt/srcradar/modules/public/algo_diff_cpp/bin/algo_diff_cpp \
    && chmod +x /opt/srcradar/modules/public/algo_diff_cpp/bin/algo_diff_cpp \
    && ls -l /opt/srcradar/modules/public/algo_diff_cpp/bin/

# ---- 3.3) mcp-server: 独立仓,构建期 clone ----
# 它是 stdlib-only 的 python daemon(daemon.py / whitelist.json / tools_schema.py),
# 镜像里不需要编译,直接 clone 源码即可。
# clone 失败不阻断构建:entrypoint 会检查它是否存在,缺失就跳过 MCP。
RUN git clone --depth 1 "${MCP_SERVER_REPO}" /tmp/srcradar-mcp-server \
    && mkdir -p /opt/srcradar/modules/public/srcradar-mcp-server \
    && cp -a /tmp/srcradar-mcp-server/. /opt/srcradar/modules/public/srcradar-mcp-server/ \
    && rm -rf /opt/srcradar/modules/public/srcradar-mcp-server/.git \
    && rm -rf /tmp/srcradar-mcp-server \
    && chmod +x /opt/srcradar/modules/public/srcradar-mcp-server/*.sh \
                /opt/srcradar/modules/public/srcradar-mcp-server/daemon.py \
    && echo "=== mcp-server in the image ===" \
    && ls /opt/srcradar/modules/public/srcradar-mcp-server

# ---- 4) DNS resolver set: cn ----
# pdtm.conf 的 resolver_set 决定用哪套 resolver。
# resolvers/cn 随仓分发在 modules/main/pdtm/resolvers/cn;
# 这里在构建期固化,让容器里的默认值就是 cn,不依赖运行时交互。
RUN mkdir -p /opt/srcradar/config && \
    printf 'resolver_set=%s\nresolvers_file=%s\n' \
        "${SRCDAR_RESOLVER}" \
        "/opt/srcradar/modules/main/pdtm/resolvers/${SRCDAR_RESOLVER}" \
        > /opt/srcradar/config/pdtm.conf && \
    echo "=== pdtm.conf ===" && cat /opt/srcradar/config/pdtm.conf

# ---- 5) 语言: zh ----
# i18n 的优先级是 SRCDAR_LANG env > config/lang.conf > en。
# ENV SRCDAR_LANG=zh 已经在上面设了;这里再写一份 lang.conf,
# 让没有继承 env 的子进程(例如 cron 起的任务)也拿到 zh。
RUN printf 'lang=%s\n' "${SRCDAR_LANG}" > /opt/srcradar/config/lang.conf && \
    echo "=== lang.conf ===" && cat /opt/srcradar/config/lang.conf

# ---- wordlists (build 时 COPY,运行时直接用,不再拉取)----
# 暴力爆破阶段用的两份 DNS 子域词表,随镜像分发,启动即用。
# 两者均为 MIT 许可(SecLists),出处与清洗规则见 NOTICE。
COPY docker/wordlists/ /opt/srcradar/wordlists/

# ---- 第三方二进制的许可证全文 (GPL-3.0 section 6)----
# massdns 是镜像里唯一由 GPL-3.0 源码构建的二进制,许可证须随其分发。
RUN mkdir -p /usr/share/doc/srcradar/licenses && \
    curl -fsSL https://www.gnu.org/licenses/gpl-3.0.txt \
         -o /usr/share/doc/srcradar/licenses/GPL-3.0.txt

# ---- ENScan 的许可证(Apache-2.0,来自构建时 clone 的 mirror)----
# The file only exists in the variant that builds ENScan, so it is mounted
# and copied conditionally.
RUN --mount=from=builder,source=/out-license,target=/out-license,ro \
    if [ -f /out-license/ENScan_GO-LICENSE ]; then \
        cp /out-license/ENScan_GO-LICENSE \
           /usr/share/doc/srcradar/licenses/ENScan_GO-LICENSE && \
        echo "the ENScan license is in the image"; \
    else \
        echo "this variant does not carry ENScan"; \
    fi

# ---- 6) entrypoint + 元数据 ----
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/healthcheck.sh /usr/local/bin/healthcheck.sh
COPY docker/MODIFICATIONS.txt /opt/srcradar/docker/MODIFICATIONS.txt
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh \
         /opt/srcradar/srcradar

LABEL org.opencontainers.image.title="srcradar" \
      org.opencontainers.image.description="SRC asset mapping + daily monitoring pipeline" \
      org.opencontainers.image.source="https://github.com/wgpsec/srcradar" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${SRCRADAR_VERSION}"

# srcradar data volume(DB + reports + snapshots + logs)

# TODO(Q4): cron 在容器内由 entrypoint 启;默认走 service cron start。
# 若想改 while-loop 模式,改 entrypoint.sh,不影响本 Dockerfile。
# WORKDIR 重要:dispatcher 调 modules/*/ 时用相对路径,必须 cd 到 srcradar root
WORKDIR /opt/srcradar
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
# 长跑模式:容器启动后保持 alive,用户用 docker exec srcradar srcradar ...
# entrypoint 已完成 init-db + cron start,这里只让容器不退出
CMD ["sleep", "infinity"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD /usr/local/bin/healthcheck.sh
