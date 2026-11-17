# Gomovie Sync — 给 Marco Movie / Gomovie 客户端用的同步后台

客户端（`android/.../CloudSync.java`）只做四件事：`GET` 一个 JSON、`PUT` 回去（带 `If-Match` / `If-None-Match` 乐观锁）、`MKCOL` 建目录、`Basic` 认证。所以任何标准 WebDAV 都能用；这个目录提供的是一个**只为它服务**的实现：单文件、零依赖、带网页管理面板。

## 为什么不用坚果云 / Nextcloud

用它们也能同步。这个后台多出来的东西只有这几样：

- **账号可自建**：用户名、密码都由你定（留空才自动生成），每台设备一个账号，面板里能看到谁什么时候读过、写过、写了多大
- **到期时间**：账号可以设到期日，到期后该账号的 WebDAV 地址立刻失效（`403 account expired`），面板里一键 +30 天或改日期
- **历史版本**：每次写入前的旧内容自动存一份，可以下载，也可以一键回滚（回滚后各设备下次同步就会拿到旧数据）
- **在线编辑**：面板里直接改同步数据 JSON 和公开的配置文件 JSON，保存前校验格式、自动留旧版本
- **请求日志**：方法/状态码/字节/IP/UA 全记，出问题能立刻看出是认证、到期、412 冲突还是网络

## 组成

```
server/
  gomovie_sync.py          服务本体（Python 3.11+，标准库）
  make_client_config.py    按账号生成客户端配置（所有源指向网关）
  test_protocol.py         自检：按客户端真实时序重放一遍，全绿才算没坏
  gomovie-sync.service     systemd 单元（就地监听 :80，非 root 用户）
  README.md                本文件
```

数据都在一个目录里（默认 `/var/lib/gomovie-sync`）：

```
config.json      secret、管理员密码哈希、public_url、保留策略
devices.json     账号 → 空间 / 密码哈希 / 启停 / 到期日 / 备注 / 网关 token
state.json       每个账号最后读取 / 最后写入时间
private/sources.json                       真实接口地址（母版，绝不公开）
public/<账号>.json                         app 导入用的配置：源地址全指向网关
data/<空间>/marco-movie-sync.json          当前同步数据
data/<空间>/versions/<时间戳>.json         历史版本
public/<名字>.json                         app「导入配置地址」用的公开配置
public/.versions/<名字>.<时间戳>           配置文件的旧版本
logs/access.jsonl                          请求日志（超过 5MB 自动轮转）
```

空间（space）就是一份同步数据。所有设备指到同一个空间 = 互相同步；给不同空间 = 互不相干。设备只能读写自己绑定的空间。

## 部署

```bash
# 1. 机器上装运行时（Debian/Ubuntu 自带 python3）
apt-get install -y python3

# 2. 放文件
install -d /opt/gomovie-sync /var/lib/gomovie-sync
useradd --system --no-create-home --shell /usr/sbin/nologin gomovie || true
install -m 755 gomovie_sync.py /opt/gomovie-sync/
install -m 644 gomovie-sync.service /etc/systemd/system/
chown gomovie:gomovie /var/lib/gomovie-sync

# 3. 初始化，记下打印出来的管理员密码（只显示一次）
su -s /bin/sh -c 'GOMOVIE_SYNC_ROOT=/var/lib/gomovie-sync \
  GOMOVIE_SYNC_URL=https://sync.example.com \
  python3 /opt/gomovie-sync/gomovie_sync.py --init' gomovie

# 4. 起服务
systemctl daemon-reload
systemctl enable --now gomovie-sync

# 5. 自检（不需要 Android，模拟客户端时序）
python3 test_protocol.py
```

服务监听 `:80`（靠 systemd 的 `CAP_NET_BIND_SERVICE`，不用 root），前面套 HTTPS 的方式见下。

## 接前端 HTTPS

**Cloudflare 橙云（当前线上用法）**：DNS 加 `sync` → 源站 IP，橙云打开，zone 的 SSL 模式设 `Flexible`，Always Use HTTPS 打开。好处是不用管证书、源站 IP 不暴露、国内访问稳定；代价是 Cloudflare → 源站这一段是明文。

**直连 + Caddy**（要端到端加密时）：把单元里的端口改成 `8765`、监听 `127.0.0.1`，前面用 Caddy 反代：

```
sync.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Caddy 自动签证书。两条路服务端代码都不用改。

## 网关：让「到期」真的能停用，而不只是停同步

客户端的配置是**导入一次、永久本地生效**的（`SourceController.fetchImport` 只在导入时请求一次）。所以只要配置里写的是真实接口地址，账号到期、后台删订阅都拦不住已经导入的人。

做法是配置里**永远不出现真实接口**：

```
config 里的 api:  https://sync.example.com/g/<该账号 token>/ffzy
                            ↓ 客户端照常带 ?ac=videolist&t=1&pg=1
服务端 /g/ 路由:   验 token → 查账号是否过期/停用 → 转发到真实 api → 原样返回 JSON
```

到期或停用后，网关直接回 403：首页列表、分类、搜索、详情全部断掉，客户端里那份缓存配置就废了。

生成配置（每个账号一份，token 不同）：

```bash
su -s /bin/sh -c 'GOMOVIE_SYNC_ROOT=/var/lib/gomovie-sync \
  python3 /opt/gomovie-sync/make_client_config.py kpad' gomovie
# 输出：已生成 …/public/kpad.json (34 条地址指向网关)
#       app 里填: https://sync.example.com/config/kpad.json
```

真实接口放在 `private/sources.json`（不对外提供），改完源要重启服务让网关重新加载。

**边界**（重要）：
- 已经拿到的**播放直链（m3u8）**仍能播那一条，网关拦不到；拦的是“继续浏览/搜索/拿新集”
- 懂技术的人抓包能看到真实源域名，理论上能自己重建配置——这是提高门槛，不是加密锁
- 网关是你的单点：服务挂了所有人都用不了（配置地址本来也在同一台机器上）
- 十几到几十人的量够用；再上量先调 `gateway_rate_per_min` 和缓存 TTL

### 限流与缓存

两次请求之间，网关做三件事：**先验账号 → 再扣令牌 → 最后才看缓存**。顺序不能反，否则已到期的账号能靠缓存继续拿数据。

| 能力 | 默认值 | 行为 |
|---|---|---|
| 每账号限流 | 120 次/分钟 | 令牌桶：允许一次性突发 120 次，之后按 2 次/秒回填；超了回 `429` + `Retry-After`，日志里 `"limited": true` |
| 站点列表缓存 | 30 秒 | 相同 `源 + 查询串` 只打一次上游，之后内存返回（响应头 `X-Cache: hit/miss`） |
| 直播列表缓存 | 300 秒 | 直播 txt/m3u 动辄几百 KB、几小时不变，单独给长 TTL |
| 缓存上限 | 64 MB | 超了先扔最早过期的条目；单条超过 16 MB 不缓存 |

只有 **200** 的响应会进缓存，403/404/502 一律不缓存——到期、上游挂掉这些必须让客户端看见，不能被旧数据盖住。

调参（改完重启服务）：

```json
{
  "gateway_rate_per_min": 120,
  "gateway_cache_ttl": 30,
  "gateway_live_cache_ttl": 300,
  "gateway_cache_bytes": 67108864
}
```

面板概览里能看到实时数字：命中 / 未命中 / 命中率 / 限流拒绝次数 / 缓存条数与占用。

### 流量

网关只转发**元数据**（列表/详情/搜索 JSON、直播订阅列表），海报和视频直链都不经过它。实测占比：

| 类型 | 请求数 | 原始流量 | 平均 |
|---|---|---|---|
| 站点元数据 | 79 | 0.72 MB | 9 KB |
| 直播列表 | 17 | **2.16 MB** | 130 KB |

直播列表占了约 3/4（单条 txt/m3u 动辄 600 KB）。所以对客户端开启了 **gzip**（客户端 `Accept-Encoding: gzip` 时，缓存里同时存原始和压缩两份，命中直接发压缩版）：

| 源 | 原始 | gzip 后 | 压缩比 |
|---|---|---|---|
| 直播 live1 | 679 KB | 147 KB | 4.6x |
| 直播 live7 | 664 KB | 100 KB | 6.6x |
| 直播 live4 | 287 KB | 25 KB | 11.7x |
| 站点 ffzy | 6 KB | 1.4 KB | 4.1x |

按 30 个用户、每人每天一次完整浏览（10 条直播 + 30 次站点请求）估算：**约 350 KB/人/天**，30 人一个月约 300 MB；不开压缩是它的 6 倍左右。这个量级对任何 VPS 都不是负担。

再想省，按性价比排：

1. **停用没用的源**——在 `private/sources.json` 里给该条加 `"disabled": true`。网关会回答 200 + 空内容（客户端不报错，只是没有频道），`make_client_config.py` 生成新配置时也会把它们剔除。适合：死链、重复、纯垃圾列表
2. 调大 `gateway_live_cache_ttl`（默认 300 秒 → 3600），减少回源次数
3. 剔除返回巨大 JSON 的站点（例如 `agg00` 单次 247 KB）
4. 不要用 Cloudflare 缓存 `/g/` 的响应——那会让到期账号在缓存期内继续可用，得不偿失

已按此停用 5 条直播列表（英文频道 679 KB、垃圾 txt 664 KB、两条死链、一条与 live2 完全重复），live 页一次完整拉取从约 1.9 MB 原始 / 300 KB 压缩降到 **73 KB**。想恢复：把 `disabled` 删掉、重启服务、重新生成配置。

## 在 app 里配置

面板「新增账号」里填：账号名、密码（留空自动生成）、空间、到期日（可空=永久）、备注。创建后会显示四项，照抄：

| 项 | 值 |
|---|---|
| 地址 | `https://sync.example.com/dav/default/marco-movie-sync.json` |
| 账号 | 你填的账号名 |
| 密码 | 你填的密码，或自动生成的那串 |
| 到期 | 到期日当天有效，次日零点起该账号地址返回 `403 account expired` |

app 里路径：设置 → 多端网络同步 → WebDAV（地址、账号、密码）。填完点「保存并立即 WebDAV 云同步」。

多台设备填同一个地址，但各自用各自的账号密码。到期日按 UTC 算，面板概览里显示「剩 N 天」，剩 7 天内标红。

## 运维

- 改管理员密码：面板「安全」区块
- 账号密码丢了：面板「重置密码」（旧密码立即失效，需在 app 里重新填）
- 续期：账号行「+30天」（从今天或原到期日中较晚的那天往后推），或「改到期」填具体日期、留空变永久
- 改数据：面板「同步数据」页直接编辑 `marco-movie-sync.json`，保存前校验 JSON，失败不会覆盖
- 改配置：面板「配置文件」页编辑 `public/*.json`，旧版本留在 `public/.versions`
- 换机器：把 `/var/lib/gomovie-sync` 整个目录拷过去即可，`config.json` 里的 secret 决定已登录会话和密码哈希
- 看某次同步为什么失败：面板「日志」，401 = 账号密码错，403 = 停用或到期，412 = 两个设备同时写（客户端会自己重试合并），404/409 = 目录没建好

## 安全边界

- 面板和同步口子都在公网，管理员密码用高强度随机串；账号密码由你自定，建议同样用随机串
- 同步数据是 JSON 明文存盘（观看进度、收藏、公开订阅源；不含网盘/Emby 凭据，客户端本来就不导出这些）
- 同一 IP 连续 15 次认证失败会被挡 5 分钟
- 单次上传上限 8MB（与客户端限制一致）
