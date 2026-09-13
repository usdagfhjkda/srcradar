# syntax=docker/dockerfile:1.7
# ============================================================================
# srcradar — multi-stage Docker build
#
# Stage 1 (builder): golang:1.25 image that builds PD toolchain + cdnmatch +
#                    db_align + ENScan (from wgpsec/ENScan_GO vendor).
# Stage 2 (runtime): ubuntu:22.04 slim with bash + python3 + sqlite3 + cron,
#                    receives only the binaries + srcradar source.
#
# Image tags (CHANGELOG):
#   ghcr.io/wgpsec/srcradar:0.1.0    — pinned srcradar release
#   ghcr.io/wgpsec/srcradar:latest   — rebuilt on git tag push (CI)
#
# Build:
#   docker build -t srcradar:dev .
#   docker build -t srcradar:0.1.0 --build-arg SRCRADAR_VERSION=0.1.0 .
#
# Q1-Q4 defaults (TODO markers; CHANGELOG next bump may revise):
#   Q1: PD tool version — pinned via PD_VERSION ARG below; bump manually
#   Q2: ENScan_GO repo  — wgpsec/ENScan_GO@v1.4.0 (matches install.sh default)
#   Q3: ENScan cookie   — volume mount + ENV fallback (see entrypoint.sh)
#   Q4: cron            — entrypoint starts service cron; daily 03:00 CST
# ============================================================================

# ----------------------------------------------------------------------------
# Stage 1: builder
# ----------------------------------------------------------------------------
FROM golang:1.25 AS builder

# 锁的是 srcradar release tag -> image 内 pdtm 版本(image 本身不可变即锁)
ARG PDTM_VERSION=v0.1.5
ARG FFUF_VERSION=v2.1.0
ARG GAU_VERSION=v2.2.3
# pingc0y/URLFinder(Linux x86_64 binary release);与 PD urlfinder 不同
ARG URLFINDER_VERSION=2026.6.16
# ENScan_GO 不再 ARG 锁上游 tag:
#   vendor 在 .vendor/enscan-go/ (commit 9969d51, 2026-03-28 by keac);
#   上游 wgpsec/ENScan_GO 已删除,后续 fork 未验证稳定性

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
COPY modules/main/pdtm/cdnmatch ./cdnmatch-src
RUN git clone --depth 1 https://github.com/projectdiscovery/cdncheck.git /tmp/cdncheck
WORKDIR /build/cdnmatch-src
RUN go mod edit -replace github.com/projectdiscovery/cdncheck=/tmp/cdncheck && \
    go mod tidy && \
    go build -o /out/cdnmatch . && \
    rm -rf /tmp/cdncheck

# ---- 3) db_align(Go orchestrator; modernc.org/sqlite 纯 Go,无 CGO)----
WORKDIR /build
COPY modules/public/db_align ./db_align-src
WORKDIR /build/db_align-src
RUN go build -o /out/db_align ./cmd/run

# ---- 4) ENScan(cloned from archival mirror,Apache-2.0)----
# Source: usdagfhjkda/wgpsec-ENScan_GO, an archival mirror of the
# deleted upstream wgpsec/ENScan_GO (commit 9969d51 by keac).
# We pin the tag wgpsec-v1.4.0-fork1 instead of a branch or SHA so
# that future upstream-archive revisions require an explicit
# Dockerfile bump, mirroring how PDTM_VERSION is pinned.
#
# The mirror's MODIFICATIONS.md is included in the clone but
# discarded after the build (rm -rf) so it does not enter the
# runtime image. Apache-2.0 LICENSE at code/LICENSE is preserved
# in the source tree we compile but is not copied to the runtime.
#
# srcradar-specific changes (qimai module) are NOT applied here;
# srcradar shells out to ENScan via db_align, and the modifications
# currently in srcradar are documented in docker/MODIFICATIONS.txt
# for the historical .vendor/ tree. Future srcradar consumer
# patches should live on top of the mirror, not inside it.
ARG ENSCAN_GO_REPO=https://github.com/usdagfhjkda/wgpsec-ENScan_GO.git
ARG ENSCAN_GO_TAG=wgpsec-v1.4.0-fork1
RUN git clone --branch "${ENSCAN_GO_TAG}" "${ENSCAN_GO_REPO}" /tmp/ENScan_GO
WORKDIR /tmp/ENScan_GO
# vendor 自带 build.sh 依赖 xgo + upx,Dockerfile 不调;直接 go build
RUN go build -o /out/ENScan . && \
    rm -rf /tmp/ENScan_GO

# ---- 5) 收集 builder 产物(pdtm 拉的 PD 工具 + 3 个自建 binary)----
# pdtm -ia 把所有 PD 工具装到 $(go env GOPATH)/bin 或 ~/.pdtm/go/bin
# 兼容两种路径:go install 默认到 /go/bin;pdtm -ia 默认到 /root/.pdtm/go/bin
RUN mkdir -p /out-bin && \
    cp /out/cdnmatch /out/db_align /out/ENScan /out-bin/ && \
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

ARG SRCRADAR_VERSION=0.1.0
ENV SRCRADAR_VERSION=${SRCRADAR_VERSION} \
    TZ=Asia/Shanghai \
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

# ---- 2) 拷贝 builder 产物(pdtm -ia 拉的 PD 工具 + 3 个自建 binary)----
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
COPY VERSION           /opt/srcradar/VERSION
COPY modules           /opt/srcradar/modules

# ---- 4) entrypoint + 元数据 ----
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
