"""兼容性通道配置模型模块（openharness.config.schema）

本模块定义了与外部通道适配器（Channel Adapter）兼容的配置数据模型。
这些模型保持同步通道适配器可导入，同时 OpenHarness 主设置系统可以独立演进。

所有模型继承自 ``_CompatModel``，允许适配器特有的额外字段透传而不报错，
确保向前兼容性。

支持的通道类型：
    - Telegram：通过 Bot Token 接入 Telegram。
    - Slack：通过 Bot Token / App Token 接入 Slack。
    - Discord：通过 Bot Token 接入 Discord。
    - Feishu（飞书）：通过 App ID / App Secret 接入。
    - DingTalk（钉钉）：通过 Client ID / Client Secret 接入。
    - Email：通过 SMTP 发送通知。
    - QQ：通过 QQ 机器人接入。
    - Matrix：通过 Homeserver 接入。
    - WhatsApp：通过 WhatsApp Business API 接入。
    - Mochat：通过自定义端点接入。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _CompatModel(BaseModel):
    """兼容性基础模型，允许适配器特有的额外字段透传。

    通过设置 Pydantic 的 extra="allow"，当通道适配器传入模型未显式定义的字段时，
    不会抛出验证错误，而是保留为额外字段。这确保了与外部通道适配器的向前兼容性，
    避免因新增字段导致旧版本崩溃。
    """

    model_config = ConfigDict(extra="allow")


class ProviderApiKeyConfig(_CompatModel):
    """API 提供商密钥配置。

    存储特定提供商的 API 密钥，用于通道适配器连接
    对应的 AI 服务后端。

    属性：
        api_key: 提供商的 API 密钥字符串，默认为空。
    """


class ProviderConfigs(_CompatModel):
    """提供商配置集合。

    汇总所有 API 提供商的密钥配置。当前仅包含 Groq 提供商，
    未来可扩展更多提供商。

    属性：
        groq: Groq 提供商的 API 密钥配置。
    """


class BaseChannelConfig(_CompatModel):
    """通道基础配置模型。

    所有通道配置类的基类，定义了通用的启用状态和来源白名单。
    安全默认设计：启用通道不会自动信任所有远程发送者，
    运维人员必须显式允许特定身份，或有意设置 ["*"] 开放访问。

    属性：
        enabled: 是否启用该通道，默认为 False。
        allow_from: 允许的发送者身份列表，默认为空（不允许任何人）。
    """


class TelegramConfig(BaseChannelConfig):
    """Telegram 通道配置。

    通过 Bot Token 接入 Telegram，支持指定目标 Chat ID。

    属性：
        token: Telegram Bot 的 API Token。
        chat_id: 目标聊天的 ID，可为 None（由运行时确定）。
    """


class SlackConfig(BaseChannelConfig):
    """Slack 通道配置。

    通过 Bot Token 和 App Token 接入 Slack 工作区，
    使用 Signing Secret 验证请求来源。

    属性：
        bot_token: Slack Bot 的 OAuth Token。
        app_token: Slack App 的 Socket Mode Token。
        signing_secret: 用于验证请求签名的密钥。
    """


class DiscordConfig(BaseChannelConfig):
    """Discord 通道配置。

    通过 Bot Token 接入 Discord 服务器。

    属性：
        token: Discord Bot 的认证 Token。
    """


class FeishuConfig(BaseChannelConfig):
    """飞书（Feishu）通道配置。

    通过 App ID / App Secret 接入飞书开放平台，
    支持加密和验证机制。

    属性：
        app_id: 飞书应用的 App ID。
        app_secret: 飞书应用的 App Secret。
        encrypt_key: 消息加密密钥。
        verification_token: 事件订阅验证 Token。
    """


class DingTalkConfig(BaseChannelConfig):
    """钉钉（DingTalk）通道配置。

    通过 Client ID / Client Secret 接入钉钉开放平台，
    使用 Robot Code 标识具体机器人实例。

    属性：
        client_id: 钉钉应用的 Client ID。
        client_secret: 钉钉应用的 Client Secret。
        robot_code: 钉钉机器人的唯一标识码。
    """


class EmailConfig(BaseChannelConfig):
    """邮件（Email）通道配置。

    通过 SMTP 协议发送通知邮件，支持 TLS 加密连接。

    属性：
        smtp_host: SMTP 服务器地址。
        smtp_port: SMTP 服务器端口，默认 587（TLS）。
        smtp_username: SMTP 认证用户名。
        smtp_password: SMTP 认证密码。
        from_address: 发件人邮箱地址。
    """


class QQConfig(BaseChannelConfig):
    """QQ 通道配置。

    通过 QQ 机器人接入 QQ 频道/群聊。

    属性：
        token: QQ 机器人的访问 Token。
        app_id: QQ 机器人的 App ID。
        app_secret: QQ 机器人的 App Secret。
    """


class MatrixConfig(BaseChannelConfig):
    """Matrix 通道配置。

    通过 Homeserver 接入 Matrix 去中心化通信网络。

    属性：
        homeserver: Matrix 服务器地址（如 https://matrix.org）。
        access_token: Matrix 用户的访问令牌。
        user_id: Matrix 用户的完整 ID（如 @bot:matrix.org）。
    """


class WhatsAppConfig(BaseChannelConfig):
    """WhatsApp 通道配置。

    通过 WhatsApp Business API 接入 WhatsApp 消息服务。

    属性：
        access_token: WhatsApp Business API 的访问令牌。
        phone_number_id: 关联的电话号码 ID。
        verify_token: Webhook 验证 Token。
    """


class MochatConfig(BaseChannelConfig):
    """Mochat 通道配置。

    通过自定义端点接入 Mochat 服务。

    属性：
        endpoint: Mochat 服务的 API 端点地址。
        token: Mochat 服务的认证 Token。
    """


class ChannelConfigs(_CompatModel):
    """通道配置集合。

    汇总所有支持的通道类型配置，以及通道的通用行为选项。

    属性：
        send_progress: 是否通过通道发送进度更新，默认为 True。
        send_tool_hints: 是否通过通道发送工具使用提示，默认为 True。
        telegram: Telegram 通道配置。
        slack: Slack 通道配置。
        discord: Discord 通道配置。
        feishu: 飞书通道配置。
        dingtalk: 钉钉通道配置。
        email: 邮件通道配置。
        qq: QQ 通道配置。
        matrix: Matrix 通道配置。
        whatsapp: WhatsApp 通道配置。
        mochat: Mochat 通道配置。
    """
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    mochat: MochatConfig = Field(default_factory=MochatConfig)


class Config(_CompatModel):
    """顶层兼容性配置模型。

    聚合通道配置和提供商配置，作为通道适配器的统一入口配置对象。
    继承自 _CompatModel，允许透传适配器特有的额外字段。

    属性：
        channels: 通道配置集合。
        providers: 提供商配置集合。
    """

