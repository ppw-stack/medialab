import { useEffect, useState } from "react";
import { Card, Table, Tag, Typography, Button, Space, Modal, Form, Input, InputNumber, Switch, message } from "antd";
import { api } from "../api";

interface Rule {
  id: number;
  name: string;
  enabled: boolean;
  priority: number;
  when: Record<string, unknown>;
  dry_run_done: boolean;
  created_at: string;
}

const { TextArea } = Input;

export default function Cleanup() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  const loadRules = () => api.get<Rule[]>("/cleanup/rules").then(setRules);

  useEffect(() => { loadRules(); }, []);

  const dryRun = async (id: number) => {
    try {
      const r = await api.post<{ matched: number }>(`/cleanup/rules/${id}/dry-run`);
      message.success(`干跑匹配 ${r.matched} 个任务`);
      loadRules();
    } catch (e) { message.error(e instanceof Error ? e.message : "失败"); }
  };

  const runRule = async (id: number) => {
    try {
      const r = await api.post<{
        candidates_executed: number;
        freed_space: number;
        pending_confirmation: number;
      }>(`/cleanup/rules/${id}/run`);
      if (r.pending_confirmation) {
        message.warning("超阈值，已进入二次确认队列");
      } else {
        message.success(`已清理 ${r.candidates_executed} 个任务`);
      }
      loadRules();
    } catch (e) { message.error(e instanceof Error ? e.message : "失败"); }
  };

  const createRule = async () => {
    const values = await form.validateFields();
    const whenStr = values.when || "{}";
    try {
      const when = JSON.parse(whenStr);
      await api.post("/cleanup/rules", { name: values.name, when, enabled: values.enabled, priority: values.priority });
      message.success("规则已创建");
      setModalOpen(false);
      form.resetFields();
      loadRules();
    } catch { message.error("JSON 格式错误"); }
  };

  const columns = [
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "优先级", dataIndex: "priority", key: "priority" },
    {
      title: "启用",
      dataIndex: "enabled",
      key: "enabled",
      render: (v: boolean) => <Tag color={v ? "green" : "default"}>{v ? "是" : "否"}</Tag>,
    },
    {
      title: "干跑",
      dataIndex: "dry_run_done",
      key: "dry_run_done",
      render: (v: boolean) => <Tag color={v ? "blue" : "orange"}>{v ? "已完成" : "待执行"}</Tag>,
    },
    {
      title: "条件",
      dataIndex: "when",
      key: "when",
      ellipsis: true,
      render: (v: Record<string, unknown>) => JSON.stringify(v),
    },
    {
      title: "操作",
      key: "action",
      render: (_: unknown, r: Rule) => (
        <Space>
          <Button size="small" onClick={() => dryRun(r.id)}>干跑</Button>
          <Button size="small" type="primary" onClick={() => runRule(r.id)}>执行</Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Typography.Title level={4}>清理中心</Typography.Title>
      <Card
        title="清理规则"
        extra={<Button type="primary" onClick={() => setModalOpen(true)}>新增规则</Button>}
      >
        <Table dataSource={rules} columns={columns} rowKey="id" pagination={false} />
      </Card>

      <Modal
        title="新增清理规则"
        open={modalOpen}
        onOk={createRule}
        onCancel={() => { setModalOpen(false); form.resetFields(); }}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="规则名" rules={[{ required: true }]}>
            <Input placeholder="如：高种比清理" />
          </Form.Item>
          <Form.Item name="when" label="条件 (JSON)">
            <TextArea
              rows={4}
              placeholder='{"ratio_gte": 3.0, "seeding_days_gte": 30}'
              defaultValue='{"ratio_gte": 3.0}'
            />
          </Form.Item>
          <Form.Item name="priority" label="优先级" initialValue={100}>
            <InputNumber />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked" initialValue={true}>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
