

本全栈工程蓝图深度融合了 **“主视角隔离”**、**“全局消息池去重”**、**“内容寻址媒体存储（MD5去重）”** 以及 **“游标滚动加载（无分页）”** 的核心底层逻辑。你可以直接复制此 Markdown 文件用于指导系统从零开发。

## 📂 1. 项目完整目录结构设计

为了保证高内聚、低耦合，同时为第二阶段的微信预留空间，项目整体工程结构规划如下：

Plaintext

```
chat-audit-core/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI 入口程序
│   ├── config.py               # 动态数据库/挂载路径环境配置
│   ├── database.py             # 异步数据库连接池初始化
│   ├── models.py               # SQLAlchemy 数据库模型 (V4 架构)
│   ├── schemas.py              # Pydantic 数据验证模型
│   ├── adapters/               # 协议适配器层 (多平台解耦)
│   │   ├── __init__.py
│   │   ├── onebot11.py         # 第一期：QQ NapCat 反向 WS 监听器
│   │   └── wechat_pc.py        # 第二期预留：微信 PC Hook 转发接收端
│   ├── services/               # 核心业务逻辑层
│   │   ├── __init__.py
│   │   ├── message_service.py  # 消息去重、主视角关联核心逻辑
│   │   ├── backup_service.py   # 细粒度条件导出与原子级覆盖导入引擎
│   │   └── dashboard_service.py# 仪表盘统计与缓存管理
│   └── static/                 # 内置前端静态资源
│       └── index.html          # 三栏式高仿 QQ/微信 Web 控制台
├── data/                       # 全量媒体资产物理存储根目录 (群晖挂载点)
│   ├── storage/                # 内容寻址存储池 (以 MD5 命名)
│   └── backups/                # 定时自动备份归档目录
├── Dockerfile                  # 内置 FFmpeg 的 Python3.10 容器配置
├── docker-compose.yml          # 多容器联调与群晖持久化编排文件
└── requirements.txt            # 项目依赖清单
```

## 🗄️ 2. 数据库模型实现 (`app/models.py`)

采用异步 SQLAlchemy 编写。将 `message_id` 设为唯一索引，用于支持跨库迁移时的 `UPSERT` 原子级覆盖。

Python

```
from sqlalchemy import Column, String, Integer, Text, DateTime, Boolean, Index
from sqlalchemy.ext.declarative import declarative_base
import datetime

Base = declarative_base()

class Adapter(Base):
    """协议端口配置表"""
    __tablename__ = 'adapters'
    
    id = Column(String(64), primary_key=True)        # 机器人账号 (QQ号/wxid)
    platform = Column(String(20), nullable=False)     # "qq" / "wechat"
    config_json = Column(Text, nullable=True)        # 动态端口/Token等配置
    status = Column(String(20), default="gray")      # "green", "red", "gray"
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class Message(Base):
    """全局消息池表 (同群消息去重核心)"""
    __tablename__ = 'messages'
    
    msg_hash = Column(String(64), primary_key=True)  # MD5(platform + room_id + sender_id + raw_message)
    platform = Column(String(20), nullable=False)
    room_id = Column(String(64), nullable=False)     # 群号/微信群ID
    message_type = Column(String(20), nullable=False)# "group" / "private"
    sender_id = Column(String(64), nullable=False)    # 发送者账号
    nickname = Column(String(128), nullable=True)    # 发送者群名片/昵称
    raw_message = Column(Text, nullable=False)       # 原始消息/CQ码
    local_message = Column(Text, nullable=False)     # 替换为本地物理哈希路径后的消息
    timestamp = Column(Integer, nullable=False, index=True) # 10位秒级时间戳
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # 建立联合索引：极大优化右侧聊天窗基于游标的向上滚动查询
    __table_args__ = (
        Index('idx_room_timestamp', 'room_id', 'timestamp'),
    )

class RobotMessage(Base):
    """主视角关联表 (多租户视角隔离)"""
    __tablename__ = 'robot_messages'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    robot_id = Column(String(64), nullable=False, index=True) # 关联账户
    msg_hash = Column(String(64), nullable=False, index=True) # 关联全局消息

class MediaAsset(Base):
    """媒体资产索引表 (内容寻址存储去重)"""
    __tablename__ = 'media_assets'
    
    file_hash = Column(String(64), primary_key=True)  # 文件的 MD5 值
    file_type = Column(String(20), nullable=False)     # "image", "video", "voice"
    file_size = Column(Integer, nullable=False)        # 体积字节数，用于仪表盘直接 SUM 计算
    local_path = Column(String(255), nullable=False)   # 物理路径
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
```

## 🛠️ 3. 后端核心去重服务 (`app/services/message_service.py`)

实现**媒体文件去重（内容寻址）**、**消息去重**以及**主视角关联绑定**的原子操作。

Python

```
import hashlib
import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.models import Message, RobotMessage, MediaAsset

class MessageService:
    @staticmethod
    def generate_md5(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()

    @staticmethod
    async def save_media_asset(db: AsyncSession, file_content: bytes, file_type: str, ext: str) -> str:
        """多媒体去重持久化逻辑"""
        file_hash = MessageService.generate_md5(file_content)
        # 统一命名并归档至大存储池中
        filename = f"{file_hash}.{ext}"
        target_path = os.path.join("data/storage", filename)
        
        # 1. 物理查重：如果文件不存在则写入硬盘
        if not os.path.exists(target_path):
            with open(target_path, "wb") as f:
                f.write(file_content)
                
        # 2. 数据库索引查重
        result = await db.execute(select(MediaAsset).where(MediaAsset.file_hash == file_hash))
        asset = result.scalar_one_or_none()
        if not asset:
            new_asset = MediaAsset(
                file_hash=file_hash,
                file_type=file_type,
                file_size=len(file_content),
                local_path=f"/static/storage/{filename}" # 映射为 Web 静态路由路径
            )
            db.add(new_asset)
            await db.commit()
            
        return f"/static/storage/{filename}"

    @staticmethod
    async def process_incoming_message(db: AsyncSession, robot_id: str, platform: str, msg_data: dict):
        """核心处理逻辑：全局去重 + 视角切片映射"""
        # 1. 计算文本消息哈希值
        raw_msg = msg_data["raw_message"]
        room_id = msg_data["room_id"]
        sender_id = msg_data["sender_id"]
        
        raw_string = f"{platform}_{room_id}_{sender_id}_{raw_msg}"
        msg_hash = hashlib.md5(raw_string.encode('utf-8')).hexdigest()
        
        # 2. 全局消息池查重 (利用 DB 锁或先查询)
        res = await db.execute(select(Message).where(Message.msg_hash == msg_hash))
        existing_msg = res.scalar_one_or_none()
        
        if not existing_msg:
            # 此时应该在异步后台下载原始 CQ 码中的网络媒体，并在此处重写 local_message，本段简写
            new_msg = Message(
                msg_hash=msg_hash,
                platform=platform,
                room_id=room_id,
                message_type=msg_data["message_type"],
                sender_id=sender_id,
                nickname=msg_data["nickname"],
                raw_message=raw_msg,
                local_message=msg_data.get("local_message", raw_msg),
                timestamp=msg_data["timestamp"]
            )
            db.add(new_msg)
            
        # 3. 无论全局消息是否存在，都必须与当前看到它的机器人账号建立主视角绑定
        assoc_res = await db.execute(
            select(RobotMessage).where(
                RobotMessage.robot_id == robot_id, 
                RobotMessage.msg_hash == msg_hash
            )
        )
        if not assoc_res.scalar_one_or_none():
            association = RobotMessage(robot_id=robot_id, msg_hash=msg_hash)
            db.add(association)
            
        await db.commit()
```

## 🎨 4. 前端三栏式 UI 架构设计 (`app/static/index.html`)

采用 Vue 3 CDN + Element Plus 构建三栏式经典 IM 布局。在最右侧聊天视窗中，使用自定义指令或原生 Scroll 监听实现**基于游标（Cursor）的时间轴向滚动历史回溯**。

HTML

```
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>社交资产多租户审计控制台</title>
    <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    <link rel="stylesheet" href="https://unpkg.com/element-plus/dist/index.css">
    <script src="https://unpkg.com/element-plus"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        /* 隐藏原生滚动条，美化 UI 交互 */
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
    </style>
</head>
<body class="bg-slate-100 h-screen overflow-hidden">
<div id="app" class="flex h-full w-full">
    
    <div class="w-20 bg-slate-900 flex flex-col items-center py-6 justify-between border-r border-slate-800">
        <div class="space-y-6 flex flex-col items-center w-full">
            <div class="text-white font-bold text-xs mb-4">AUDIT V4</div>
            <div v-for="acc in accountList" :key="acc.id" 
                 @click="switchAccount(acc)"
                 :class="['p-1 rounded-full cursor-pointer transition', currentRobot.id === acc.id ? 'ring-2 ring-emerald-500' : '']">
                <img :src="'https://q.qlogo.cn/g?b=qq&nk=' + acc.id + '&s=640'" class="w-12 h-12 rounded-full">
            </div>
        </div>
        <div class="text-slate-400 hover:text-white cursor-pointer" @click="activeTab = 'settings'">
            <i class="el-icon-setting"></i>设置
        </div>
    </div>

    <div class="w-80 bg-white border-r border-slate-200 flex flex-col" v-if="activeTab === 'chat'">
        <div class="p-4 border-b border-slate-100">
            <el-input v-model="searchQuery" placeholder="全维搜索：群聊/联系人/聊天记录..." clearable @input="handleSearch"></el-input>
        </div>
        <div class="flex-1 overflow-y-auto">
            <div v-for="room in roomList" :key="room.room_id" 
                 @click="selectRoom(room)"
                 :class="['flex items-center px-4 py-3 cursor-pointer hover:bg-slate-50 transition', currentRoom.room_id === room.room_id ? 'bg-slate-100' : '']">
                <img :src="'https://p.qlogo.cn/gh/' + room.room_id + '/' + room.room_id + '/640'" class="w-10 h-10 rounded-lg mr-3">
                <div class="flex-1 min-w-0">
                    <p class="text-sm font-medium text-slate-800 truncate">{{ room.room_id }}</p>
                    <p class="text-xs text-slate-400 truncate">最近存盘: {{ room.last_time }}</p>
                </div>
            </div>
        </div>
    </div>

    <div class="flex-1 bg-slate-50 flex flex-col h-full" v-if="activeTab === 'chat' && currentRoom.room_id">
        <div class="h-14 bg-white border-b border-slate-200 flex items-center justify-between px-6">
            <span class="font-semibold text-slate-700">{{ currentRoom.room_id }}</span>
            <div class="space-x-2">
                <el-button size="small" type="primary" plain @click="openExportModal">高级过滤导出</el-button>
            </div>
        </div>
        
        <div ref="chatWindow" class="flex-1 overflow-y-auto p-6 space-y-4" @scroll="handleWindowScroll">
            <div v-for="msg in messageList" :key="msg.msg_hash" 
                 :class="['flex w-full items-start', msg.sender_id === currentRobot.id ? 'flex-row-reverse' : '']">
                <img :src="'https://q.qlogo.cn/g?b=qq&nk=' + msg.sender_id + '&s=640'" class="w-10 h-10 rounded-full mx-2">
                <div :class="['flex flex-col max-w-[65%]', msg.sender_id === currentRobot.id ? 'items-end' : 'items-start']">
                    <span class="text-xs text-slate-400 mb-1">{{ msg.nickname }} ({{ msg.sender_id }})</span>
                    <div :class="['p-3 rounded-xl text-sm shadow-sm leading-relaxed break-all', msg.sender_id === currentRobot.id ? 'bg-emerald-500 text-white rounded-tr-none' : 'bg-white text-slate-800 rounded-tl-none']">
                        <span v-if="!isMedia(msg.local_message)">{{ msg.local_message }}</span>
                        <div v-else>
                            <el-image v-if="isImg(msg.local_message)" :src="getMediaUrl(msg.local_message)" :preview-src-list="[getMediaUrl(msg.local_message)]" class="max-w-xs rounded"></el-image>
                            <video v-if="isVideo(msg.local_message)" :src="getMediaUrl(msg.local_message)" controls class="max-w-sm rounded"></video>
                            <div v-if="isVoice(msg.local_message)" @click="playAudio(msg.local_message)" class="flex items-center space-x-2 cursor-pointer">
                                <span>🎵 语音条播放</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    const { createApp, ref, onMounted, nextTick } = Vue;
    createApp({
        setup() {
            const activeTab = ref('chat');
            const accountList = ref([{id: '12345678'}, {id: '87654321'}]);
            const currentRobot = ref({id: '12345678'});
            const roomList = ref([{room_id: '789456123', last_time: '2026-07-02'}]);
            const currentRoom = ref({});
            const messageList = ref([]);
            const chatWindow = ref(null);
            const loadingHistory = ref(false);
            
            // 📡 核心：游标向上无限滚动检测
            const handleWindowScroll = async () => {
                if (chatWindow.value.scrollTop === 0 && !loadingHistory.value && messageList.value.length > 0) {
                    loadingHistory.value = true;
                    // 1. 获取屏幕最顶部那条消息的时间戳作为游标
                    const cursorTimestamp = messageList.value[0].timestamp;
                    
                    // 2. 记住加载前容器的物理高度
                    const oldScrollHeight = chatWindow.value.scrollHeight;
                    
                    // 3. 异步向后端请求该时间节点前的50条更老数据
                    const oldMessages = await fetchOlderMessagesFromServer(cursorTimestamp);
                    
                    if (oldMessages.length > 0) {
                        messageList.value = [...oldMessages, ...messageList.value];
                        // 4. 核心锁死：数据拼接后，利用差值拨回滚动条，避免视口发生向下跳动闪烁
                        await nextTick();
                        chatWindow.value.scrollTop = chatWindow.value.scrollHeight - oldScrollHeight;
                    }
                    loadingHistory.value = false;
                }
            };

            return { activeTab, accountList, currentRobot, roomList, currentRoom, messageList, chatWindow, handleWindowScroll };
        }
    }).use(ElementPlus).mount('#app');
</script>
</body>
</html>
```

## 📅 5. 敏捷开发推进流程清单与部署指导

### 阶段一：打通 QQ 存储管道 (第 1-2 周)

- [ ] 按照清单 1 初始化全量目录，配置 Docker 物理映射目录。
    
- [ ] 导入 SQLAlchemy 模型设计（清单 2），选择 PostgreSQL/MySQL 进行建表初始化。
    
- [ ] 部署测试用 NapCatQQ 镜像，将其配置中的 `reverse_ws` 路径指向本机的 `ws://宿主机IP:8000/onebot/v11/ws`。
    
- [ ] 完善多媒体异步下载器与 `ffmpeg` 自动转码引擎，在控制台观察群发多媒体时哈希去重的落盘情况。
    

### 阶段二：组装全维检索与三栏控制台前端 (第 3 周)

- [ ] 编写基于时间的游标分页 API：`GET /api/messages?before_timestamp=xxx&limit=50`。
    
- [ ] 将高仿网页（清单 4）挂载到 FastAPI 的静态资源目录。
    
- [ ] 实测多账号同群聊天，验证左侧随意切换账号头像时，中间列群聊视角能实时隔离查阅。
    
- [ ] 验证向上滚动时，老消息无感追加、滚动条锚点不抖动的交互体验。
    

### 阶段三：高级数据治理扩展（条件导出导入、自动化备份）(第 4 周)

- [ ] 实现带系统识别码签名机制的 `export/import` 服务，利用各个数据库特有的 **Upsert 关键字** 完成覆盖回录测试。
    
- [ ] 在系统【设置】页面测试设定：每天凌晨 3:00 自动触发归档。
    
- [ ] **完成微信扩展扩展前置准备**：验证此时给接口发一组 `robot_id: "wxid_123"` 且平台为 `wechat` 的字符串测试消息，底层系统依然可以完全兼容读取，完成第一阶段全部闭环。