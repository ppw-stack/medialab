import { useEffect, useState } from "react";
import {
  Card, Tabs, Form, InputNumber, Switch, Input, Button, Space, Table, Tag, message, Modal, Popconfirm, Select, Typography,
} from "antd";
import { api } from "../api";
import { useAuth } from "../AuthProvider";

// 渠道类型 → 必填字段提示（前端在配置时显示，但不写死服务端）
const channelTypeHint: Record<string, { name: string; configFields: string[]; needsSecret: boolean }> = {
  wecom:      { name: "企业微信群机器人", configFields: [], needsSecret: true },
  serverchan: { name: "Server 酱",       configFields: [], needsSecret: true },
  telegram:   { name: "Telegram Bot",    configFields: ["chat_id"], needsSecret: true },
  email:      { name: "邮件 SMTP",       configFields: ["host", "port", "username", "from", "to"], needsSecret: true },
  bark:       { name: "Bark (iOS)",      configFields: ["host"], needsSecret: true },
  webhook:    { name: "Webhook",         configFields: ["url"], needsSecret: false },
};

export default function Settings() {
  const { username } = useAuth();

  return (
    <div>
      <Typography.Title level={4} style={{ marginBottom: 16 }}>系统设置（{username}）</Typography.Title>
      <Tabs
        defaultActiveKey="alert"
        items={[
          { key: "alert", label: "告警策略", children: <AlertPolicyTab /> },
          { key: "channels", label: "通知渠道", children: <ChannelsTab /> },
          { key: "scheduler", label: "调度作业", children: <SchedulerTab /> },
        ]}
      />
    </div>
  );
}

// ============================================================ 告警策略
interface AlertPolicy {
  mute_window_hours: number;
  warning_daily_digest: boolean;
  critical_immediate: boolean;
  storm_threshold: number;
  quiet_hours: string | null;
}

function AlertPolicyTab() {
  const [form] = Form.useForm<AlertPolicy>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const p = await api.get<AlertPolicy>("/alerts/policy");
      form.setFieldsValue(p);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const onSave = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      await api.put("/alerts/policy", values);
      message.success("已保存");
    } finally { setSaving(false); }
  };

  return (
    <Card loading={loading}>
      <Form form={form} layout="vertical" style={{ maxWidth: 600 }}>
        <Form.Item label="静默窗口 (小时)" name="mute_window_hours" tooltip="同 hash + 同类别在该窗口内不重复推送">
          <InputNumber min={0} max={168} />
        </Form.Item>
        <Form.Item label="🟡 警告每日汇总一次" name="warning_daily_digest" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="🔴 严重立即推送" name="critical_immediate" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="风暴保护阈值（单日告警数）" name="storm_threshold">
          <InputNumber min={1} max={10000} />
        </Form.Item>
        <Form.Item label="静默时段 (HH:MM-HH:MM)" name="quiet_hours" tooltip="支持跨零点如 23:00-07:00；🔴 始终不受影响">
          <Input placeholder="如 23:00-07:00，留空=无" />
        </Form.Item>
        <Form.Item>
          <Button type="primary" onClick={onSave} loading={saving}>保存</Button>
        </Form.Item>
      </Form>
    </Card>
  );
}

// ============================================================ 通知渠道
interface Channel {
  id: number; name: string; type: string; enabled: boolean; min_level: string;
  config: Record<string, unknown>; has_secret: boolean;
  fail_count: number; breaker_until: string | null; last_ok_at: string | null; last_error: string | null;
}

const LEVELS = [
  { value: "info", label: "🔵 info" },
  { value: "warning", label: "🟡 warning" },
  { value: "critical", label: "🔴 critical" },
];

function ChannelsTab() {
  const [data, setData] = useState<Channel[]>([]);
  const [editing, setEditing] = useState<Channel | null>(null);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm();
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error: string } | null>(null);

  const load = () => api.get<Channel[]>("/alerts/channels").then(setData);
  useEffect(() => { load(); }, []);

  const onCreate = () => { setEditing(null); setCreating(true); form.resetFields(); };
  const onEdit = (c: Channel) => { setEditing(c); setCreating(false); form.setFieldsValue(c); setTestResult(null); };
  const onDelete = async (id: number) => {
    await api.del(`/alerts/channels/${id}`);
    message.success("已删除");
    load();
  };

  const onTest = async () => {
    if (!editing) return;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.post<{ ok: boolean; error: string }>(`/alerts/channels/${editing.id}/test`);
      setTestResult(r);
      if (r.ok) message.success("渠道测试成功"); else message.error(`测试失败：${r.error}`);
    } finally { setTesting(false); }
    load();
  };

  const onSave = async () => {
    const values = await form.validateFields();
    if (creating) {
      await api.post("/alerts/channels", values);
      message.success("渠道已创建");
    } else if (editing) {
      await api.put(`/alerts/channels/${editing.id}`, values);
      message.success("渠道已更新");
    }
    setEditing(null);
    setCreating(false);
    form.resetFields();
    load();
  };

  const columns = [
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "类型", dataIndex: "type", key: "type",
      render: (v: string) => {
        const hint = channelTypeHint[v];
        return <Tag color="blue">{hint?.name || v}</Tag>;
      },
    },
    {
      title: "启用", dataIndex: "enabled", key: "enabled",
      render: (v: boolean) => <Tag color={v ? "green" : "default"}>{v ? "是" : "否"}</Tag>,
    },
    { title: "最低级别", dataIndex: "min_level", key: "min_level" },
    {
      title: "熔断", key: "breaker",
      render: (_: unknown, r: Channel) => r.breaker_until && new Date(r.breaker_until) > new Date()
        ? <Tag color="red">熔断中（{r.fail_count} 失败）</Tag>
        : <Tag color="green">正常</Tag>,
    },
    {
      title: "状态", key: "status",
      render: (_: unknown, r: Channel) => r.last_error
        ? <Tag color="red" title={r.last_error}>异常</Tag>
        : <Tag color="green">正常</Tag>,
    },
    {
      title: "操作", key: "action",
      render: (_: unknown, r: Channel) => (
        <Space>
          <Button size="small" onClick={() => onEdit(r)}>编辑</Button>
          <Popconfirm title="确认删除？" onConfirm={() => onDelete(r.id)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const currentType = (Form.useWatch("type", form) || "webhook") as string;
  const hint = channelTypeHint[currentType];

  return (
    <Card
      title="通知渠道"
      extra={<Button type="primary" onClick={onCreate}>新增渠道</Button>}
    >
      <Table dataSource={data} columns={columns} rowKey="id" pagination={false} />

      <Modal
        open={creating || editing !== null}
        title={creating ? "新增渠道" : `编辑渠道 - ${editing?.name || ""}`}
        onCancel={() => { setCreating(false); setEditing(null); setTestResult(null); }}
        onOk={onSave}
        width={640}
      >
        <Form form={form} layout="vertical">
          <Form.Item label="名称" name="name" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="类型" name="type" rules={[{ required: true }]}>
            <Select disabled={!creating}>
              {Object.entries(channelTypeHint).map(([v, h]) => (
                <Select.Option key={v} value={v}>{h.name}</Select.Option>
              ))}
            </Select>
          </Form.Item>
          {hint && hint.configFields.length > 0 && (
            <Form.Item label="配置 (JSON)" name="config" tooltip={`必填字段：${hint.configFields.join(", ")}`}>
              <Input.TextArea
                rows={4}
                placeholder={JSON.stringify(
                  Object.fromEntries(hint.configFields.map((f) => [f, ""])),
                  null,
                  2,
                )}
                onChange={(e) => {
                  try {
                    form.setFieldValue("config", JSON.parse(e.target.value));
                  } catch { /* ignore until valid */ }
                }}
              />
            </Form.Item>
          )}
          {hint && !hint.configFields.length && (
            <Form.Item name="config" hidden>
              <Input />
            </Form.Item>
          )}
          <Form.Item
            label={hint?.needsSecret ? "密钥 / Token" : "认证头（可选）"}
            name="secret"
            tooltip={editing?.has_secret ? "已设置；留空表示不修改" : "Webhook 模式下作为 Authorization 头值"}
          >
            <Input.Password
              placeholder={editing?.has_secret ? "已设置（留空保留）" : ""}
            />
          </Form.Item>
          <Form.Item label="最低接收级别" name="min_level">
            <Select options={LEVELS} />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked" initialValue={false}>
            <Switch />
          </Form.Item>
        </Form>

        {editing && (
          <Card size="small" style={{ marginTop: 16, background: "#1f1f1f" }}>
            <Space>
              <Button onClick={onTest} loading={testing}>发送测试消息</Button>
              {testResult && (
                <Tag color={testResult.ok ? "green" : "red"}>
                  {testResult.ok ? "✓ 成功" : `✗ 失败：${testResult.error}`}
                </Tag>
              )}
            </Space>
          </Card>
        )}
      </Modal>
    </Card>
  );
}

// ============================================================ 调度作业
interface Job {
  name: string;
  interval_seconds: number;
  enabled: boolean;
  running: boolean;
}
interface SchedulerInfo {
  enabled: boolean;
  jobs: Job[];
  inflight: number;
  accepting_new_work: boolean;
}

const JOB_LABELS: Record<string, { name: string; desc: string }> = {
  cross_seed:     { name: "辅种拉取",     desc: "FR-2：每小时从各站点 RSS 拉取一次（受 FR-2.2 频率下限约束）" },
  queue_replay:   { name: "队列重放",     desc: "§6：每 5 分钟消费 task_queue 中 pending 任务（崩溃恢复）" },
  inspect:        { name: "巡检",         desc: "FR-4.1：每 15 分钟巡检全部任务 + 触发告警" },
  free_seed_poll: { name: "免费种拉取",   desc: "FR-3：每 10 分钟捕获免费种候选 + 添加到下载器" },
  window_watch:   { name: "窗口看护",     desc: "FR-3.4：每 60 秒检查在途免费种，按紧迫度调整" },
  cleanup_aborts: { name: "中止处理",     desc: "M5：每 5 分钟消费 M4 中止产物走 Cleaner 管线" },
};

function SchedulerTab() {
  const [data, setData] = useState<SchedulerInfo | null>(null);

  const load = () => api.get<SchedulerInfo>("/scheduler").then(setData).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);

  const toggle = async (name: string, enabled: boolean) => {
    await api.post(`/scheduler/${name}/toggle?enabled=${enabled}`);
    message.success(`${name} → ${enabled ? "启用" : "暂停"}`);
    load();
  };

  if (!data) return <Card><Typography.Text>加载中...</Typography.Text></Card>;

  return (
    <Card
      title={`调度器（${data.accepting_new_work ? "工作中" : "停机中"}，在途 ${data.inflight}）`}
    >
      <Table
        dataSource={data.jobs}
        rowKey="name"
        pagination={false}
        columns={[
          { title: "作业", dataIndex: "name", key: "name", render: (v: string) => {
              const lbl = JOB_LABELS[v];
              return lbl ? <><div><b>{lbl.name}</b></div><Typography.Text type="secondary" style={{ fontSize: 12 }}>{v}</Typography.Text></> : v;
            }
          },
          { title: "说明", key: "desc", render: (_: unknown, r: Job) => JOB_LABELS[r.name]?.desc || "-" },
          { title: "间隔", dataIndex: "interval_seconds", key: "interval", render: (v: number) => v >= 60 ? `${v / 60}min` : `${v}s` },
          { title: "运行中", dataIndex: "running", key: "running", render: (v: boolean) => v ? <Tag color="processing">是</Tag> : <Tag>否</Tag> },
          {
            title: "启用", dataIndex: "enabled", key: "enabled",
            render: (v: boolean, r: Job) => (
              <Switch checked={v} onChange={(e) => toggle(r.name, e)} />
            ),
          },
        ]}
      />
      {!data.enabled && (
        <Typography.Paragraph type="warning" style={{ marginTop: 16 }}>
          调度器已禁用（SCHEDULER_ENABLED=false）。所有周期性作业都不会执行。
        </Typography.Paragraph>
      )}
    </Card>
  );
}
