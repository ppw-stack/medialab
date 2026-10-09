import { useEffect, useState } from "react";
import {
  Card, Table, Tag, Button, Space, Modal, Form, Input, InputNumber, Select, Switch, message, Popconfirm, Typography, Tooltip, Tag as AntTag,
} from "antd";
import { api } from "../api";

interface Site {
  id: number; name: string; domain: string; adapter: string;
  enabled: boolean;
  rss_url_masked: string;
  default_free_duration: number;  // seconds
  min_buffer: number;             // seconds
  buffer_ratio: number;
  clock_offset: number;            // seconds
  clock_offset_probed_at: string | null;
  rescue_strategy: string;
  grab_short_window: boolean;
}

const ADAPTERS = [
  { value: "generic", label: "GenericSiteAdapter (推荐)" },
];

const RESCUE_STRATEGIES = [
  { value: "notify_only", label: "仅告警（默认，最安全）" },
  { value: "speed_up", label: "提速：临时清除下载限速" },
  { value: "yield_then_retry", label: "让路后重试（让上传优先）" },
  { value: "abort_now", label: "立即中止（提前止损）" },
];

const formatDuration = (s: number) => {
  if (s < 3600) return `${Math.round(s / 60)}min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
};

export default function Sites() {
  const [data, setData] = useState<Site[]>([]);
  const [editing, setEditing] = useState<Site | null>(null);
  const [creating, setCreating] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ status: string; detail: string; cap: object } | null>(null);
  const [form] = Form.useForm();

  const load = () => api.get<Site[]>("/sites").then(setData).catch(() => {});
  useEffect(() => { load(); }, []);

  const onCreate = () => {
    setEditing(null);
    setCreating(true);
    form.resetFields();
    form.setFieldsValue({
      adapter: "generic",
      default_free_duration: 72,
      min_buffer: 30,
      buffer_ratio: 0.10,
      min_interval_minutes: 60,
      enabled: true,
      save_path_template: "/data/torrents/{site}/{name}",
      rescue_strategy: "notify_only",
      grab_short_window: false,
    });
    setTestResult(null);
  };
  const onEdit = (s: Site) => {
    setEditing(s);
    setCreating(false);
    form.setFieldsValue({
      ...s,
      default_free_duration: s.default_free_duration / 3600,   // 显示为小时
      min_buffer: s.min_buffer / 60,                          // 显示为分钟
    });
    setTestResult(null);
  };

  const onDelete = async (id: number) => {
    await api.del(`/sites/${id}`);
    message.success("已删除");
    load();
  };

  const onSave = async () => {
    const v = await form.validateFields();
    const payload = {
      ...v,
      // 后端用 parse_duration_seconds 解析这些字段
      default_free_duration: v.default_free_duration ? `${v.default_free_duration}h` : "72h",
      min_buffer: v.min_buffer ? `${v.min_buffer}m` : "30m",
    };
    if (creating) {
      await api.post("/sites", payload);
      message.success("已创建");
    } else if (editing) {
      await api.put(`/sites/${editing.id}`, payload);
      message.success("已更新");
    }
    setCreating(false);
    setEditing(null);
    load();
  };

  const onTest = async () => {
    if (!editing) return;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.post<{ ok: boolean; status: string; detail: string; self_test: object }>(
        `/sites/${editing.id}/feed-test`,
      );
      setTestResult({
        status: r.status,
        detail: r.detail,
        cap: r.self_test,
      });
    } finally { setTesting(false); }
  };

  const onClockAdjust = async (id: number, delta: number) => {
    try {
      await api.post(`/sites/${id}/clock-offset`, { delta_seconds: delta });
      message.success(`时钟偏差调整 ${delta}s`);
      load();
    } catch (e) { message.error(e instanceof Error ? e.message : "失败"); }
  };

  const columns = [
    { title: "名称", dataIndex: "name", key: "name", render: (v: string, r: Site) => (
        <Space direction="vertical" size={0}>
          <b>{v}</b>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>{r.domain}</Typography.Text>
        </Space>
    ) },
    { title: "适配器", dataIndex: "adapter", key: "adapter", render: (v: string) => <Tag>{v}</Tag> },
    { title: "RSS", dataIndex: "rss_url_masked", key: "rss", render: (v: string) => (
      <Tooltip title={v}><Typography.Text code style={{ fontSize: 12 }}>{v || "-"}</Typography.Text></Tooltip>
    ) },
    {
      title: "时钟偏差", dataIndex: "clock_offset", key: "clock", render: (s: number, r: Site) => (
        <Space size={4}>
          <Tag color={s > 60 ? "red" : s < -60 ? "orange" : "green"}>
            {s > 0 ? "+" : ""}{s}s
          </Tag>
          <Tooltip title="微调（站点偏快则 -）">
            <Button size="small" onClick={() => onClockAdjust(r.id, -1)}>−1s</Button>
          </Tooltip>
          <Button size="small" onClick={() => onClockAdjust(r.id, 1)}>+1s</Button>
        </Space>
      ),
    },
    { title: "免费窗口", dataIndex: "default_free_duration", key: "dur", render: formatDuration },
    { title: "缓冲", dataIndex: "buffer_ratio", key: "buf", render: (v: number) => `${(v * 100).toFixed(0)}%` },
    {
      title: "抢救", dataIndex: "rescue_strategy", key: "rs", render: (v: string) => {
        const label = RESCUE_STRATEGIES.find((r) => r.value === v)?.label || v;
        return <Tag color="purple">{label}</Tag>;
      },
    },
    {
      title: "启用", dataIndex: "enabled", key: "enabled", render: (v: boolean) =>
        v ? <Tag color="green">✓</Tag> : <Tag>—</Tag>,
    },
    {
      title: "操作", key: "action", render: (_: unknown, r: Site) => (
        <Space>
          <Button size="small" onClick={() => onEdit(r)}>编辑</Button>
          <Button size="small" onClick={() => onTest()}>测试</Button>
          <Popconfirm title="确认删除？" onConfirm={() => onDelete(r.id)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Typography.Title level={4}>站点管理</Typography.Title>
      <Card
        extra={<Button type="primary" onClick={onCreate}>新增站点</Button>}
        title="FR-2.1 站点列表（凭据脱敏）"
      >
        <Table
          dataSource={data}
          columns={columns}
          rowKey="id"
          pagination={{ pageSize: 20 }}
        />
      </Card>

      <Modal
        open={creating || editing !== null}
        title={creating ? "新增站点" : `编辑站点 - ${editing?.name || ""}`}
        onCancel={() => { setCreating(false); setEditing(null); setTestResult(null); }}
        onOk={onSave}
        width={720}
      >
        <Form form={form} layout="vertical">
          <Space style={{ width: "100%" }} size="middle">
            <Form.Item label="名称" name="name" rules={[{ required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: "字母/数字/下划线/连字符" }]} style={{ flex: 1 }}>
              <Input />
            </Form.Item>
            <Form.Item label="域名" name="domain" rules={[{ required: true }]} style={{ flex: 1 }}>
              <Input placeholder="example-pt.com" />
            </Form.Item>
          </Space>
          <Form.Item label="RSS URL（含 passkey，存库前加密）" name="rss_url">
            <Input.Password placeholder="https://example.com/rss.php?feedkey=..." />
          </Form.Item>
          <Form.Item label="保存路径模板" name="save_path_template" tooltip="支持占位符：{site} {name} {category} {info_hash}">
            <Input />
          </Form.Item>
          <Space style={{ width: "100%" }} size="middle">
            <Form.Item label="适配器" name="adapter" style={{ flex: 1 }}>
              <Select options={ADAPTERS} />
            </Form.Item>
            <Form.Item label="RSS 最小间隔 (分钟)" name="min_interval_minutes" tooltip="FR-2.2：下限 15 分钟">
              <InputNumber min={15} max={1440} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item label="体积上限 (GB)" name="max_size" tooltip="辅种过滤；空=不限">
              <InputNumber min={0} style={{ width: "100%" }} />
            </Form.Item>
          </Space>
          <Typography.Title level={5} style={{ marginTop: 8 }}>免费窗口（FR-3.0.1）</Typography.Title>
          <Space style={{ width: "100%" }} size="middle">
            <Form.Item label="默认窗口 (小时)" name="default_free_duration" tooltip="该站无法解析窗口时的兜底值">
              <InputNumber min={1} max={720} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item label="安全余量 (分钟)" name="min_buffer" tooltip="max(窗口×ratio, min_buffer)">
              <InputNumber min={1} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item label="余量比例" name="buffer_ratio">
              <InputNumber min={0.01} max={0.5} step={0.01} style={{ width: "100%" }} />
            </Form.Item>
          </Space>
          <Form.Item
            label="短窗口强制抓取" name="grab_short_window" valuePropName="checked"
            tooltip="剩余 < 90min 时通常跳过；开启后即使短也抓"
          >
            <Switch />
          </Form.Item>
          <Form.Item label="抢救策略 (FR-3.8)" name="rescue_strategy" tooltip="到红线时执行：通知/提速/让路/立即中止">
            <Select options={RESCUE_STRATEGIES} />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>

        {editing && (
          <Card size="small" style={{ marginTop: 16, background: "#1f1f1f" }}>
            <Space>
              <Button onClick={onTest} loading={testing}>RSS 测试 + 窗口解析自检</Button>
              {testResult && (
                <AntTag color={testResult.status === "ok" ? "green" : "red"}>
                  {testResult.status} | {testResult.detail}
                </AntTag>
              )}
            </Space>
          </Card>
        )}
      </Modal>
    </div>
  );
}
