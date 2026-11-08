// 最小的 zip 读取：只支持不加密、存储（0）或 deflate（8）的普通 zip，够读服务端打的投稿包和系统「压缩」出来的包。
// 解压用浏览器自带的 DecompressionStream，不需要第三方库。
(() => {
  if (globalThis.__wmpvpReadZip) return;

  const MIME = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", gif: "image/gif", webp: "image/webp", json: "application/json" };

  function mimeOf(name) {
    return MIME[(name.split(".").pop() || "").toLowerCase()] || "application/octet-stream";
  }

  // 返回 Map<路径, () => Promise<Blob>>
  async function readZip(file) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let eocd = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 65535); i--) {
      if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("不是有效的 zip 文件");
    const count = view.getUint16(eocd + 10, true);
    let p = view.getUint32(eocd + 16, true);
    const decoder = new TextDecoder("utf-8");
    const entries = new Map();
    for (let n = 0; n < count; n++) {
      if (view.getUint32(p, true) !== 0x02014b50) throw new Error("zip 目录损坏");
      const flags = view.getUint16(p + 8, true);
      const method = view.getUint16(p + 10, true);
      const size = view.getUint32(p + 20, true);
      const nameLength = view.getUint16(p + 28, true);
      const extraLength = view.getUint16(p + 30, true);
      const commentLength = view.getUint16(p + 32, true);
      const local = view.getUint32(p + 42, true);
      const name = decoder.decode(bytes.subarray(p + 46, p + 46 + nameLength));
      p += 46 + nameLength + extraLength + commentLength;
      if (name.endsWith("/") || name.startsWith("__MACOSX/")) continue;
      if (flags & 1) throw new Error(`zip 里的 ${name} 加了密码，不支持`);
      if (method !== 0 && method !== 8) throw new Error(`zip 里的 ${name} 压缩方式不支持（${method}）`);
      const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
      const data = bytes.subarray(start, start + size);
      const type = mimeOf(name);
      entries.set(name, async () => {
        if (method === 0) return new Blob([data], { type });
        const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
        return new Blob([await new Response(stream).arrayBuffer()], { type });
      });
    }
    return entries;
  }

  globalThis.__wmpvpReadZip = readZip;
})();
