// Pure bounded projection shared by the Pi adapter and offline Node tests.
export function projectStructured(value) {
  const omissions = {};
  function visit(item, path, depth = 0) {
    if (typeof item === "string") {
      const maximum = depth <= 1 ? 1200 : 600;
      if (item.length > maximum) omissions[path] = { total_characters: item.length, omitted_characters: item.length - maximum };
      return item.length > maximum ? item.slice(0, maximum) + "…" : item;
    }
    if (!item || typeof item !== "object") return item;
    const list = Array.isArray(item);
    const entries = list ? item : Object.entries(item);
    const maximum = depth >= 6 ? 0 : (list ? 8 : 20);
    if (entries.length > maximum) omissions[path] = { total: entries.length, returned: maximum, omitted: entries.length - maximum };
    if (list) return entries.slice(0, maximum).map((entry, i) => visit(entry, `${path}/${i}`, depth + 1));
    return Object.fromEntries(entries.slice(0, maximum).map(([key, entry]) => [key, visit(entry, `${path}/${key}`, depth + 1)]));
  }
  return { value: visit(value || {}, ""), omissions };
}
