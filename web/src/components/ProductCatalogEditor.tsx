import React, { useState } from "react";
import { useApp } from "../context/AppContext";
import { ProductMetric } from "../types";
export const ProductCatalogEditor: React.FC<{ products: ProductMetric[] }> = ({
  products,
}) => {
  const { activeTenant, token, triggerRefresh } = useApp();
  const [pid, setPid] = useState(""),
    [name, setName] = useState(""),
    [sku, setSku] = useState(""),
    [message, setMessage] = useState(""),
    [saving, setSaving] = useState(false);
  async function save() {
    if (!activeTenant || !pid) return;
    setSaving(true);
    try {
      const r = await fetch(`/tenants/${activeTenant.id}/products/${pid}`, {
        method: "PUT",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ product_name: name, sku: sku || null }),
      });
      if (!r.ok)
        throw new Error(`${r.status}: ${JSON.stringify(await r.json())}`);
      setMessage("Catalog saved. Financial history unchanged.");
      triggerRefresh();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setSaving(false);
    }
  }
  return (
    <details className="p-4 rounded-2xl bg-[#07261d] border border-emerald-900">
      <summary className="font-bold cursor-pointer">
        Maintain Product Names / SKUs
      </summary>
      <div className="space-y-3 pt-3">
        <select
          aria-label="Catalog product"
          value={pid}
          onChange={(e) => {
            setPid(e.target.value);
            const p = products.find((p) => p.product_id === e.target.value);
            setName(p?.product_name || "");
            setSku(p?.sku || "");
          }}
          className="w-full bg-[#03150f] p-2"
        >
          <option value="">Select a ranked product</option>
          {products.map((p) => (
            <option key={p.product_id} value={p.product_id}>
              {p.product_name || "Uncataloged product"} · {p.product_id}
            </option>
          ))}
        </select>
        <input
          aria-label="Product name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Product name"
          className="bg-[#03150f] p-2"
        />
        <input
          aria-label="Product SKU"
          value={sku}
          onChange={(e) => setSku(e.target.value)}
          placeholder="SKU"
          className="bg-[#03150f] p-2"
        />
        <button
          disabled={saving || !pid || !name.trim()}
          onClick={save}
          className="bg-emerald-700 px-4 py-2 rounded-xl"
        >
          Save Catalog
        </button>
        <p aria-live="polite">{message}</p>
      </div>
    </details>
  );
};
