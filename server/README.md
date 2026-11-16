# Gomovie Sync — 给 Marco Movie / Gomovie 客户端用的同步后台

客户端（`android/.../CloudSync.java`）只做四件事：`GET` 一个 JSON、`PUT` 回去（带 `If-Match` / `If-None-Match` 乐观锁）、`MKCOL` 建目录、`Basic` 认证。所以任何标准 WebDAV 都能用；这个目录提供的是一个**只为它服务**的实现：单文件、零依赖、带网页管理面板。

## 为什么不用坚果云 / Nextcloud

用它们也能同步。这个后台多出来的东西只有三样：

- **每台设备一个账号**：面板里能看到是哪台设备、什么时候读过、什么时候写过、写了多大
- **历史版本**：每次写入前的旧内容自动存一份，可以下载，也可以一键回滚（回滚后各设备下次同步就会拿到旧数据）
- **请求日志**：方法/状态码/字节/IP/UA 全记，出问题能立刻看出是认证、412 冲突还是网络

## 组成

```
server/
  gomovie_sync.py       服务本体（Python 3.11+，标准库）
  test_protocol.py      自检：按客户端真实时序重放一遍，全绿才算没坏
  gomovie-sync.service  systemd 单元（就地监听 :80，非 root 用户）
  README.md             本文件
```

数据都在一个目录里（默认 `/var/lib/gomovie-sync`）：

```
config.json      secret、管理员密码哈希、public_url、保留策略
devices.json     设备 → 空间 / 密码哈希 / 启停
state.json       每台设备最后读取 / 最后写入时间
data/<空间>/marco-movie-sync.json          当前同步数据
data/<空间>/versions/<时间戳>.json         历史版本
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

## 在 app 里配置

面板里新建设备后会显示四项，照抄：

| 项 | 值 |
|---|---|
| 地址 | `https://sync.example.com/dav/default/marco-movie-sync.json` |
| 账号 | 设备名 |
| 密码 | 创建时生成的那串 |

app 里路径：设置 → 多端网络同步 → WebDAV（地址、账号、密码）。填完点「保存并立即 WebDAV 云同步」。

多台设备填同一个地址，但各自用各自的账号密码。

## 运维

- 改管理员密码：面板「安全」区块
- 设备密码丢了：面板「重置密码」（旧密码立即失效，需在 app 里重新填）
- 换机器：把 `/var/lib/gomovie-sync` 整个目录拷过去即可，`config.json` 里的 secret 决定已登录会话和密码哈希
- 看某次同步为什么失败：面板「日志」，401 = 账号密码错，412 = 两个设备同时写（客户端会自己重试合并），404/409 = 目录没建好

## 安全边界

- 面板和同步口子都在公网，管理员密码和每台设备的密码都是随机生成的高强度串
- 同步数据是 JSON 明文存盘（观看进度、收藏、公开订阅源；不含网盘/Emby 凭据，客户端本来就不导出这些）
- 同一 IP 连续 15 次认证失败会被挡 5 分钟
- 单次上传上限 8MB（与客户端限制一致）
