# Render 免费方案部署说明

使用 Render 免费 Web Service + Neon 免费 PostgreSQL。Neon 中保存工作台账号（密码哈希）、任务检查点、导出礼包码表格和操作审计；这些数据属于敏感数据，只有授权的 Neon 和 Render 管理员应能访问。Render 的本地文件是临时的，重启时从数据库恢复任务文件。数据库连接不可用时，不会退回临时账号。服务重启后内存中的下载按钮不会自动恢复，可核对后台后按原参数恢复任务。数据库服务不提供绝对永久保障，需监控容量、可用性并定期安全备份与验证恢复。

## 准备数据库
1. 在 https://neon.com/ 注册并创建 Free PostgreSQL 项目。保存好账号，创建后在 Neon 控制台找到数据库连接字符串；它包含密码，禁止提交 GitHub、发给他人或粘贴到聊天。
2. 在 Render 现有 Web Service 的 Environment 中设置以下环境变量：
   - `WORKBENCH_CLOUD=1`
   - `WORKBENCH_PUBLIC_HOST=你的实际服务.onrender.com`（不含 https://、端口和路径）
   - `DATABASE_URL=Neon 提供的 PostgreSQL 连接字符串`（必须保密；程序会验证数据库 TLS 证书和主机名）
   - `WORKBENCH_SECRET=至少32字符的随机密钥`（保密且长期保持不变；修改后已有会话失效）
   - `WORKBENCH_ADMIN_PASSWORD=至少16字符的强随机密码`（管理员账号 admin-zpc，不写入仓库）
3. 可选：`WORKBENCH_DATA_DIR=/tmp/workbench-data`；这是临时缓存目录，不是数据库存储位置。只有单实例、单进程得到支持。
4. 验收前不要切换正式服务或合并 main：先使用独立测试服务、测试数据库和测试后台验证连接、账号增删、重启后保存、生成与恢复；测试服务可能也占免费额度。用户正在执行任务时不要部署。
5. 旧账号不会自动迁移。更新前应先备份旧版有效账号数据，确认当前实例上仍存在 `.auth_users.json` 并采用安全迁移方案。免费 Render 可能已经因重启或休眠清除该文件。不能保证丢失账号可恢复。

Render 免费实例会休眠且不能挂载持久磁盘，免费 Render Postgres 30 天到期，不能用于该长期方案。Neon 免费套餐有容量、计算和恢复限制，见其当前条款；额度不足或服务停止可能影响登录、发码和数据恢复。请自行监控与备份，不保证永久免费或永久保存。

## Supabase 备用方案

如果 Neon 控制台无法使用，可以创建一个专用的 Supabase Free 项目，并关闭该项目的 Data API。到项目的 **Connect → Session pooler** 复制 PostgreSQL 连接字符串，将其中的密码占位符替换为实际数据库密码，填入 Render 的 `DATABASE_URL`。再从 Supabase 的 **Database Settings → SSL Configuration** 下载 CA 证书，在 Render 的 **Environment → Secret Files** 中保存为 `supabase-ca.crt`，并设置 `WORKBENCH_DB_CA_FILE=/etc/secrets/supabase-ca.crt`。其他 Render 环境变量沿用上述配置。Supabase 免费项目在一周内活动不足时可能暂停，需要到控制台恢复。

## 安全和操作
Render URL 仍可能被公网访问，工作台登录只控制认证，不等于网络隔离；上线前应增加零信任入口、限流和审计，并阻止绕过入口。普通用户具备完整业务操作权限，本人任务状态、预览和下载不会暴露给其他用户。管理员凭据、DATABASE_URL、SECRET 均只放 Render 环境变量，不可写入 GitHub。云端不保存 GMS 凭据；预览和生成时需要保持页面中的后台账号密码。

真实发码依赖 GMS 证书和接口，尚未实际连线上后台验证。停止会等待当前请求，已经发出的操作可能生效，出错后先到后台核对。数据库写入失败时可能出现“后台操作已生效但进度记录未保存”，恢复前必须人工核对；不要盲目重发。
