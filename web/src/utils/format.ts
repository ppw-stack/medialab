/**
 * 字节大小格式化（统一实现，消除各页面 fmtBytes / formatSize 重复）。
 *
 * 默认行为（decimals = 2）：大数值场景（上传量 / 下载量）用
 *   "1.35 TB" / "50.54 GB"
 *
 * decimals = 1 + integerBytes：任务列表场景（种子体积 / 速率）用
 *   "500 B" / "1.5 KB" / "1.0 MB"
 *   —— 字节数是整数时不显示小数，宽度更紧凑。
 */
export interface FormatBytesOptions {
  /** 小数位数（默认 2） */
  decimals?: number;
  /** 字节级整数时是否省略小数（默认 false） */
  integerBytes?: boolean;
}

export function formatBytes(
  b: number,
  opts: FormatBytesOptions = {},
): string {
  const decimals = opts.decimals ?? 2;
  const integerBytes = opts.integerBytes ?? false;
  if (!b) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const abs = Math.abs(b);
  if (abs < 1) {
    // |b| < 1：Math.log 会取到负指数，导致单位索引为负 → undefined
    return b.toFixed(decimals) + " B";
  }
  const i = Math.floor(Math.log(abs) / Math.log(1024));
  // 防御：超出 units 范围（极大值）时取最大单位
  const idx = Math.min(Math.max(i, 0), units.length - 1);
  const value = b / Math.pow(1024, idx);
  // 字节单位 + 整数数值 → 不显示小数（"500 B" 而非 "500.00 B"）
  const text =
    integerBytes && idx === 0 && Number.isInteger(value)
      ? String(value)
      : value.toFixed(decimals);
  return text + " " + units[idx];
}

/**
 * 时间格式化（北京时间字符串原样展示）。
 *
 * 后端 DB 时间列已统一为北京时间字符串（``yyyy-MM-dd HH:mm:ss``），
 * 故**不做二次时区转换**——直接展示，避免「看起来对但差 8 小时」的坑。
 *
 * 空值文案由调用方给（``"—"`` / ``"从未"`` 等场景不同）。
 */
export function formatTime(
  ts: string | null | undefined,
  empty = "—",
): string {
  if (!ts) return empty;
  return ts;
}
