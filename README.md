小白龙视频点播网站 - 部署说明
================================

## 一、安装依赖

```bash
cd D:\wangzhan
pip install -r requirements.txt
```

## 二、启动网站

```bash
python app.py
```

访问: http://127.0.0.1:5000
管理后台: http://127.0.0.1:5000/admin
管理员账号: admin / admin888

## 三、功能说明

### 前台功能
- 首页：视频列表展示
- 视频播放：支持HTML5流式播放
- 15秒试看：非会员只能看15秒，时间到弹出收款二维码
- 会员系统：注册→登录→购买会员→免限制观看
- 四方支付：支持支付宝/微信/QQ钱包

### 后台管理
- 系统概览：视频数、用户数、会员数、订单数、收入
- 视频管理：上传/删除视频，支持本地和远程视频
- 订单管理：查看所有支付记录
- 支付配置：配置四方支付接口

## 四、四方支付对接

在管理后台 » 支付配置中填写：
- API URL：四方支付网关地址
- App ID：商户ID
- App Secret：商户密钥
- Notify URL：异步通知地址（需公网可访问）
- Return URL：支付成功跳转地址

如不填将使用演示模式。

## 五、部署到服务器（推荐）

```bash
# 使用gunicorn部署
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app

# 配合nginx反向代理
# 前端静态文件用nginx托管，API请求代理到5000端口
```

## 六、目录结构

```
D:\wangzhan\
├── app.py                 # 后端主程序
├── requirements.txt       # 依赖
├── static/
│   ├── css/
│   │   ├── style.css      # 前台样式
│   │   └── admin.css      # 后台样式
│   ├── js/
│   │   └── main.js        # 通用脚本
│   ├── uploads/           # 封面图上传
│   └── qrcodes/           # 收款码
├── templates/             # HTML模板
│   ├── base.html
│   ├── index.html
│   ├── video.html
│   ├── login.html
│   ├── register.html
│   ├── member.html
│   ├── pay_success.html
│   ├── admin_base.html
│   ├── admin_login.html
│   ├── admin_dashboard.html
│   ├── admin_videos.html
│   ├── admin_orders.html
│   └── admin_pay.html
├── data/                  # SQLite数据库（自动创建）
└── videos/                # 视频文件存储
```
