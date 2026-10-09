import { useEffect, useState } from "react";
import {
  Card, Table, Tag, Space, Select, Input, Tooltip, Modal, Typography, Statistic, Row, Col,
} from "antd";
import { api } from "../api";

interface Torrent {
  id: number; info_hash: string; name: string; site: string; category: string | null;
  status: string; source: string; size: number; downloaded: number; uploaded: number;
  ratio: number; state: string | null; is_free: boolean;
  free_type: string | null; free_end_at: string | null; added_at: string;
  protect_until: string | null;
}

interface AuditEntry {
  id: number; ts: string; action: string; trigger_source: string;
  hash_count: number; freed_space: number; result: string; reason: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  active: "green", paused: "orange", completed: "blue", aborted: "red", deleted: "gray",
  free_completed: "green", free_completed_late: "orange", free_missed: "red",
};

const SOURCE_LABEL: Record<string, string> = {
  auto_seed: "辅种",
  free_seed: "免费种",
  manual: "手动",
};

const formatSize = (b: number) => {
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`;
  if (b < 1024 ** 4) return `${(b / 1024 ** 3).toFixed(1)} GB`;
  return `${(b / 1024 ** 4).toFixed(2)} TB`;
};

const formatDate = (d: string | null) => d ? new Date(d).toLocaleString() : "-";

export default function Torrents() {
  const [data, setData] = useState<{ total: number; items: Torrent[] }>({ total: 0, items: [] });
  const [filters, setFilters] = useState<{ site_id?: number; source?: string; status?: string; q?: string }>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [detail, setDetail] = useState<Torrent | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [stats, setStats] = useState<{ by_status: Record<string, number>; by_source: Record<string, number> } | null>(null);

  const load = () => {
    const qs = new URLSearchParams();
    if (filters.site_id) qs.set("site_id", String(filters.site_id));
    if (filters.source) qs.set("source", filters.source);
    if (filters.status) qs.set("status", filters.status);
    if (filters.q) qs.set("page_size", "200");
    api.get<{ total: number; items: Torrent[] }>(`/torrents?${qs.toString()}&page_size=200`)
      .then(setData)
      .catch(() => {});
  };
  useEffect(() => { load(); }, [filters]);

  useEffect(() => {
    api.get<{ torrents: { by_status: Record<string, number>; by_source: Record<string, number> } }>("/dashboard")
      .then((d) => setStats({ by_status: d.torrents.by_status, by_source: d.torrents.by_source }))
      .catch(() => {});
  }, []);

  const onShowDetail = async (t: Torrent) => {
    setDetail(t);
    try {
      const r = await api.get<{ items: AuditEntry[] }>(`/torrents/audit?limit=50`);
      setAudit(r.items.filter((a) => a.action === "add_torrent" || a.action === "trash" || a.action === "remove"));
    } catch { setAudit([]); }
  };

  // 客户端筛选（搜索）覆盖服务端筛选
  const filtered = filters.q
    ? data.items.filter((t) => t.name.toLowerCase().includes(filters.q!.toLowerCase()) || t.info_hash.startsWith(filters.q!))
    : data.items;
  const pageItems = filtered.slice((page - 1) * pageSize, page * pageSize);

  const columns = [
    { title: "种子", dataIndex: "name", key: "name", ellipsis: true, render: (v: string, r: Torrent) => (
        <Space direction="vertical" size={0}>
          <a onClick={() => onShowDetail(r)} style={{ fontWeight: 500 }}>{v}</a>
          <Typography.Text type="secondary" style={{ fontSize: 11 }} code>{r.info_hash.slice(0, 16)}…</Typography.Text>
        </Space>
    ) },
    { title: "站点", dataIndex: "site", key: "site" },
    {
      title: "来源", dataIndex: "source", key: "source",
      render: (v: string) => <Tag color={v === "free_seed" ? "purple" : v === "auto_seed" ? "blue" : "default"}>{SOURCE_LABEL[v] || v}</Tag>,
    },
    {
      title: "状态", dataIndex: "status", key: "status",
      render: (v: string) => <Tag color={STATUS_COLOR[v] || "default"}>{v}</Tag>,
    },
    {
      title: "免费", dataIndex: "free_type", key: "free",
      render: (v: string | null, r: Torrent) => !r.is_free ? "-" :
        <Tag color={v === "freeleech_permanent" ? "green" : "orange"}>{v}</Tag>,
    },
    { title: "大小", dataIndex: "size", key: "size", render: formatSize },
    { title: "进度", key: "progress", render: (_: unknown, r: Torrent) => {
        const pct = r.size ? Math.round(r.downloaded / r.size * 100) : 0;
        return <Tooltip title={`${formatSize(r.downloaded)} / ${formatSize(r.size)}`}>
          <span>{pct}%</span>
        </Tooltip>;
      }
    },
    { title: "种比", dataIndex: "ratio", key: "ratio", render: (v: number) => v.toFixed(2) },
    { title: "添加于", dataIndex: "added_at", key: "added", render: formatDate },
  ];

  return (
    <div>
      <Typography.Title level={4}>任务管理</Typography.Title>

      {stats && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col span={8}>
            <Card size="small">
              <Statistic title="活跃任务" value={stats.by_status.active || 0} />
            </Card>
          </Col>
          <Col span={8}>
            <Card size="small">
              <Statistic title="暂停" value={stats.by_status.paused || 0} />
            </Card>
          </Col>
          <Col span={8}>
            <Card size="small">
              <Statistic title="在途免费种" value={stats.by_source.free_seed || 0} />
            </Card>
          </Col>
        </Row>
      )}

      <Card
        title={`任务列表（总计 ${data.total}）`}
        extra={
          <Space>
            <Input.Search
              placeholder="搜索名称 / hash"
              allowClear
              onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
              onSearch={(v) => setFilters((f) => ({ ...f, q: v }))}
              style={{ width: 240 }}
            />
            <Select
              placeholder="来源"
              allowClear
              style={{ width: 120 }}
              value={filters.source}
              onChange={(v) => setFilters((f) => ({ ...f, source: v }))}
              options={[
                { value: "auto_seed", label: "辅种" },
                { value: "free_seed", label: "免费种" },
                { value: "manual", label: "手动" },
              ]}
            />
            <Select
              placeholder="状态"
              allowClear
              style={{ width: 140 }}
              value={filters.status}
              onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
              options={[
                { value: "active", label: "active" },
                { value: "paused", label: "paused" },
                { value: "aborted", label: "aborted" },
                { value: "deleted", label: "deleted" },
                { value: "free_completed", label: "free_completed" },
              ]}
            />
          </Space>
        }
      >
        <Table
          dataSource={pageItems}
          columns={columns}
          rowKey="id"
          pagination={{
            current: page,
            pageSize,
            total: filtered.length,
            showSizeChanger: true,
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          }}
        />
      </Card>

      <Modal
        open={detail !== null}
        onCancel={() => setDetail(null)}
        title={detail?.name || ""}
        footer={null}
        width={720}
      >
        {detail && (
          <div>
            <Row gutter={[16, 8]}>
              <Col span={8}><Typography.Text type="secondary">Hash</Typography.Text></Col>
              <Col span={16}><code style={{ fontSize: 12 }}>{detail.info_hash}</code></Col>
              <Col span={8}><Typography.Text type="secondary">站点</Typography.Text></Col>
              <Col span={16}>{detail.site}</Col>
              <Col span={8}><Typography.Text type="secondary">分类</Typography.Text></Col>
              <Col span={16}>{detail.category || "-"}</Col>
              <Col span={8}><Typography.Text type="secondary">大小 / 已下 / 种比</Typography.Text></Col>
              <Col span={16}>
                {formatSize(detail.size)} / {formatSize(detail.downloaded)} / {detail.ratio.toFixed(2)}
              </Col>
              <Col span={8}><Typography.Text type="secondary">来源 / 状态</Typography.Text></Col>
              <Col span={16}>
                <Tag color="blue">{SOURCE_LABEL[detail.source] || detail.source}</Tag>
                <Tag color={STATUS_COLOR[detail.status] || "default"}>{detail.status}</Tag>
                {detail.is_free && <Tag color="purple">{detail.free_type}</Tag>}
              </Col>
              {detail.free_end_at && (
                <>
                  <Col span={8}><Typography.Text type="secondary">免费窗口截止</Typography.Text></Col>
                  <Col span={16}>{formatDate(detail.free_end_at)}</Col>
                </>
              )}
              {detail.protect_until && (
                <>
                  <Col span={8}><Typography.Text type="secondary">禁删保护截止</Typography.Text></Col>
                  <Col span={16}>{formatDate(detail.protect_until)}</Col>
                </>
              )}
            </Row>

            <Typography.Title level={5} style={{ marginTop: 24 }}>审计 (近 50 条)</Typography.Title>
            <Table
              dataSource={audit}
              pagination={false}
              size="small"
              rowKey="id"
              columns={[
                { title: "时间", dataIndex: "ts", key: "ts", render: formatDate, width: 160 },
                { title: "动作", dataIndex: "action", key: "action" },
                { title: "触发源", dataIndex: "trigger_source", key: "ts2" },
                { title: "数量", dataIndex: "hash_count", key: "count", width: 60 },
                { title: "释放", dataIndex: "freed_space", key: "free", render: formatSize },
                {
                  title: "结果", dataIndex: "result", key: "r",
                  render: (v: string) => <Tag color={v === "done" ? "green" : "red"}>{v}</Tag>,
                },
              ]}
            />
          </div>
        )}
      </Modal>
    </div>
  );
}
