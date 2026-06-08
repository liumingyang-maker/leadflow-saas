# LeadFlow 部署手册

代码已经改造成「容器化、环境变量配置、数据存持久卷」，可以一键部署。
下面两条路线二选一。**不懂命令行就选 Zeabur**。

---

## 一、要准备的环境变量（两个平台通用）

部署时在平台后台填这几个（**不要把密钥写进代码上传**）：

| 变量名 | 填什么 | 必填 |
|------|------|:---:|
| `SMTP_USER` | 平台系统邮箱账号（发注册验证/找回密码用），如 `447507531@qq.com` | ✅ |
| `SMTP_PASS` | 该邮箱的 SMTP 授权码 | ✅ |
| `SITE_URL` | 部署后的公网网址，如 `https://leadflow.xxx.com`（**末尾不要斜杠**） | ✅ |
| `SECRET_KEY` | 一段长随机字符串（固定不变，别人不知道即可） | ✅ |
| `SMTP_HOST` | 默认 `smtp.qq.com`，用别的邮箱才改 | ⬜ |
| `SMTP_PORT` | 默认 `465` | ⬜ |
| `DATA_DIR` | `/data`（Dockerfile 已默认，一般不用填） | ⬜ |

> `SITE_URL` 很重要：开发信里的链接、邮件打开追踪像素都用它，填错追踪和链接就不对。
> 域名要先解析到平台给的地址，配好 HTTPS 后再把 `SITE_URL` 改成 `https://...`。

数据存放：数据库和客户数据都在容器的 `/data` 目录，**必须挂一个持久卷到 `/data`**，
否则重新部署数据会丢。

---

## 二、路线 A：Zeabur（推荐，最省事，自动 HTTPS）

1. 打开 https://zeabur.com 登录（用 GitHub 账号登录最方便）。
2. New Project → 选区域，建议选 **香港 / 新加坡**（境外节点，部分采集更顺）。
3. Add Service → Git → 授权并选你的仓库 `liumingyang-maker/leadflow-saas`。
   - Zeabur 会自动识别根目录的 `Dockerfile` 来构建，无需额外配置。
4. 进服务的 **Variables（环境变量）**，把上面表格里的变量挨个填进去。
5. 加持久卷：服务的 **Volumes** → 新建一个卷，**挂载路径填 `/data`**。
6. 绑定域名：服务的 **Networking/Domains** → 用 Zeabur 送的免费子域名，
   或绑你自己的域名（按提示加一条 CNAME 解析）。HTTPS 自动签发。
7. 拿到正式网址后，把环境变量 `SITE_URL` 改成这个 https 网址，重新部署一次。
8. 打开 `网址/` 注册体验；管理后台 `网址/admin`（admin@leads.com / admin123，**登录后请改密码**）。

以后改了代码 `git push`，Zeabur 会自动重新部署。

---

## 三、路线 B：阿里云 ECS（你自己的服务器，需要命令行）

前提：一台能 SSH 的 Linux 服务器，装好 Docker。**建议香港/新加坡地域**
（境内 IP 跑 Zauba/Europages/yt-dlp 这类采集会被墙或限速；绑域名还要 ICP 备案）。

```bash
# 1. 拉代码
git clone https://github.com/liumingyang-maker/leadflow-saas.git
cd leadflow-saas

# 2. 建数据目录 + 写环境变量文件（只在服务器上，不上传）
mkdir -p /opt/leadflow-data
cat > .env.prod <<'EOF'
SMTP_USER=你的邮箱
SMTP_PASS=你的SMTP授权码
SITE_URL=https://你的域名
SECRET_KEY=换成一长串随机字符
EOF

# 3. 构建并运行（数据挂到宿主机 /opt/leadflow-data，重启不丢）
docker build -t leadflow .
docker run -d --name leadflow --restart always \
  -p 8080:8080 \
  --env-file .env.prod \
  -e DATA_DIR=/data \
  -v /opt/leadflow-data:/data \
  leadflow
```

4. **HTTPS**：前面架个 Nginx 反向代理到 `127.0.0.1:8080`，用 certbot 申请免费证书。
   （或用阿里云的 SLB/证书服务。）
5. 安全组放行 80/443。绑域名要 ICP 备案（境内）或用境外地域免备案。

更新：`git pull && docker build -t leadflow . && docker rm -f leadflow && docker run ...`（同上）。

---

## 四、部署后务必做

- [ ] 改掉管理后台默认密码（admin@leads.com / admin123）。
- [ ] 确认 `SITE_URL` 是最终 https 域名。
- [ ] 发一封测试注册邮件，确认系统邮件能发出（SMTP 配对）。
- [ ] **数据备份**：定期把持久卷里的 `admin.db` 和 `tenants/` 打包备份到对象存储
      （客户数据是命根子，别只存一份）。
- [ ] 让客户在「系统设置」里配自己的采集/发信 API Key。
