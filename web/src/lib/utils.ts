const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
  CAD: "CA$",
  AUD: "AU$",
};

export function formatCurrency(
  amount?: string | number | null,
  currency: string = "USD",
): string {
  if (amount == null) return "—";
  const raw =
    typeof amount === "number"
      ? Number.isFinite(amount)
        ? amount.toFixed(2)
        : ""
      : amount;
  const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(raw);
  if (!match) return "—";
  const [, sign, whole, fraction = ""] = match;
  const grouped = BigInt(whole)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}${CURRENCY_SYMBOLS[currency] || `${currency} `}${grouped}.${fraction.padEnd(2, "0")}`;
}

export function formatNumber(n?: string | number | null): string {
  if (n == null) return "0";
  const num = typeof n === "string" ? parseFloat(n) : n;
  if (isNaN(num)) return "0";
  return num.toLocaleString("en-US");
}

export function formatDate(dateStr?: string | null): string {
  if (!dateStr) return "—";
  try {
    return new Date(dateStr).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    });
  } catch {
    return dateStr;
  }
}

export function formatDateTime(dateStr?: string | null): string {
  if (!dateStr) return "—";
  try {
    return (
      new Date(dateStr).toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZone: "UTC",
        hour12: false,
      }) + " UTC"
    );
  } catch {
    return dateStr;
  }
}

export function relativeTime(date: Date | null): string {
  if (!date) return "never";
  const secs = Math.floor((Date.now() - date.getTime()) / 1000);
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ago`;
}

export function truncateId(id?: string | null, len: number = 8): string {
  if (!id) return "";
  return id.length <= len ? id : `${id.slice(0, len)}...`;
}

export function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.setAttribute("href", url);
  link.setAttribute("download", filename);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

/** Exact positive-money division, rounded half up to a cent. */
export function averageMoney(amount: string, count: number): string | null {
  if (!Number.isSafeInteger(count) || count <= 0) return null;
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(amount);
  if (!match) return null;
  const total =
    BigInt(match[1]) * 100n + BigInt((match[2] || "").padEnd(2, "0"));
  const divisor = BigInt(count);
  const rounded = (2n * total + divisor) / (2n * divisor);
  return `${rounded / 100n}.${(rounded % 100n).toString().padStart(2, "0")}`;
}
