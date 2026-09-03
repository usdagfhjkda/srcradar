# ymicp 小程序备案查询(集成层)

> **模块定位**:集成层 plugin,**可选用**。通过 [`icp_mapp_query.py`](./icp_mapp_query.py) 调用独立的 [HG-ha / ICP_Query](https://github.com/HG-ha/ICP_Query) 服务,反查企业 / 子公司的小程序 / 公众号备案。

## 导航

- **本文档**:§模块边界 / §第三方依赖声明 / §部署 / §客户端使用教程 / §运维 / §文件说明
- **客户端**:本文 §使用 srcradar 客户端
- **服务端依赖**:**非 srcradar 维护**,由用户自部署 — 见 §部署
- **运维(Ops)**:见本文 §运维(客户端异常 / 服务端异常 / 集成)
- **与 db_align/pdtm 关系**(可选,无硬依赖):
  - 写表:`mapp_records`(小程序 / 公众号 备案)
  - 与 `db_align`:`db_align` 拉回法律实体(`companies`),`ymicp` 反查这些实体的备案;**数据互补,流程上无先后硬依赖**
  - 与 `pdtm`:完全独立,不读写对方表
- **跳过它的场景**:不需要小程序 / 公众号备案反查时,直接忽略整个 `ymicp/` 目录;`db_align` / `pdtm` / `daily` 不依赖它

> **⚠️ 重要声明**:本目录**仅**包含 srcradar 编写的 Python HTTP 客户端。
> 它调用的是由第三方独立维护的 ICP_Query / ymicp 服务;
> srcradar **不重新分发、不维护、不背书**该服务端。

---

## 一、模块边界

| 项 | 范围 | License |
|---|---|---|
| **本目录的 `icp_mapp_query.py`** | srcradar 原创客户端 | Apache-2.0 |
| **ymicp / ICP_Query 服务端** | 由第三方独立维护,**非 srcradar 维护** | ⚠️ 原仓库未声明开源协议 |

**srcradar 的职责**:
- 提供 Python HTTP 客户端,封装 `/query/mapp` 调用
- 数据持久化到 `../db/recon.sqlite3`

**srcradar 不做**:
- 不打包服务端
- 不主动拉服务端 Docker 镜像
- 不提供服务端技术支持/合规背书

---

## 二、第三方依赖声明

| 服务 | 提供方 | 仓库 | License | 维护关系 |
|---|---|---|---|---|
| **ymicp / ICP_Query** | 一铭 / HG-ha | https://github.com/HG-ha/ICP_Query | ⚠️ **未声明**(GitHub 默认视为 All rights reserved) | **非 srcradar 维护**,使用前请自行评估合规风险 |

原项目 README 中明确写明:

> 开源目的仅学习交流逆向与验证码识别技术使用

启用 ymicp 服务前请自行评估:
- License 不明的法律风险
- 使用范围限制(原项目声明仅学习交流)
- 验证码识别模块的合规性

---

## 三、部署 ymicp 服务(**用户自行处理**)

srcradar **不提供** ymicp 服务端的自动化部署。请按以下方式之一自行部署:

### 方案 A:使用 Docker Hub 镜像(默认方式)

```bash
# 注意: yiminger/ymicp 镜像与 srcradar 维护者无关,
#       使用前请确认镜像来源可信
docker run -d -p 127.0.0.1:16181:16181 --name ymicp yiminger/ymicp
```

镜像默认监听 `127.0.0.1:16181`(仅本机)。`srcradar/icp_mapp_query.py` 的 `--base` 默认就是这个地址。

### 方案 B:从源码自行 build

参考 https://github.com/HG-ha/ICP_Query 的构建说明(Rust 版本 `icpApi-rs` 或 Python 版本)。

### 方案 C:不用 ymicp

如果你不需要小程序备案反查,**直接忽略整个 ymicp/ 目录**。srcradar 其他模块(`db_align`、`pdtm`、`daily`)不依赖 ymicp。

---

## 四、使用 srcradar 客户端

启动 ymicp 服务后,运行客户端查询:

```bash
# 单个公司
echo "ExampleCo子公司有限公司" | python3 icp_mapp_query.py

# 批量(每行:业务名|公司名,或仅公司名)
python3 icp_mapp_query.py < companies.txt

# 自定义参数
echo "TestBiz" | python3 icp_mapp_query.py     --base http://127.0.0.1:16181     --db ../db/recon.sqlite3
```

参数:

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base` | `http://127.0.0.1:16181` | ymicp 服务地址 |
| `--user` / `--pass` | 空 | ymicp 启用了 auth 时使用 |
| `--db` | `../db/recon.sqlite3` | SQLite 输出路径(与 db_align / pdtm 共享) |
| `--delay` | 5s | 请求间隔(强制延时,详见 ymicp 服务说明) |

---

## 五、运维(Ops)

ymicp 是**集成层 plugin**,自己只编写客户端 (`icp_mapp_query.py`),服务端由第三方独立维护。运维分两部分:**客户端异常** 和 **服务端异常**。

### 客户端异常(`icp_mapp_query.py`)

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `Connection refused` 到 `127.0.0.1:16181` | ymicp 服务未启动,或 `--base` 指向错地址 | 起服务端;或重新指 `--base` |
| HTTP 401/403 | 服务端启用了 auth,客户端没传 `--user` / `--pass` | 加上 `--user` / `--pass` |
| 持续 `Empty result` | aqc 服务端因 cookie 失效(走的是同一个 ENScan 类生态);或公司名错 | 先换更精确公司名测试;若所有都空 → 排查服务端 |
| `database is locked` | 别的进程在写 `../db/recon.sqlite3`(pdtm/daily/ymicp/dashboard 都可能) | 等;WAL + busy_timeout 5s 自动重试 |
| 写入含 `synth:` 的 licence (`db_align` 已合成过) | 同一公司反查撞 UNIQUE 约束 | **正常**,服务端的 APP/微信/微博 section 无 ICP 备案号时由 `db_align` 合成,ymicp 跳过 |

### 服务端异常(`127.0.0.1:16181` 那一侧)

**重要**:srcradar **不维护**这个服务端,以下问题需要用户自行排查:

- 服务端日志(取决于部署方式:Docker `docker logs ymicp`;源码方式启动的看进程的 stdout)
- 验证码识别模块(`HG-ha/ICP_Query` 含):合规性 / 准确率 / ToS 由用户自评
- 端口冲突:`ss -tlnp | grep 16181` 看谁占,改 `--base` 到别的端口

### 与 daily 流水线集成

`daily/run_one_business.sh -type icp` 把 ymicp 客户端作为流水线第三阶段拉起。失败不阻塞后续阶段,bit-mask exit code `bit 2 (4)` 标识 icp 失败。详见 [`../daily/README.md`](../daily/README.md)。

### 不启用 ymicp

- `recon_business_config.icp = 0`(或 `set_config.sh --icp 0`),该业务的 icp 阶段直接跳过
- 不部署服务端:ymicp 客户端会快速 fail,bit 2 置位,但不影响其它阶段

---

## 六、文件说明

| 文件 | 说明 |
|---|---|
| `icp_mapp_query.py` | srcradar 客户端(Apache-2.0),本目录唯一保留的文件 |
| ~~`docker-compose.yml`~~ | ❌ 已移除(由用户自行部署) |
| ~~`config.yml`~~ | ❌ 已移除(ymicp 服务端配置由用户维护) |
| `ymicp.sqlite3` | 历史遗留,新结构已迁到 `../db/recon.sqlite3` |

---

