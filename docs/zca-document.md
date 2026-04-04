# zca-js – Unofficial Zalo API for JavaScript

**Tài liệu đầy đủ & chi tiết**  
**Phiên bản thư viện**: 2.x (cập nhật 2026)  
**Tác giả**: RFS-ADRENO  
**GitHub**: [https://github.com/RFS-ADRENO/zca-js](https://github.com/RFS-ADRENO/zca-js)  
**Docs chính thức**: [https://zca-js.tdung.com/vi](https://zca-js.tdung.com/vi) | [English](https://zca-js.tdung.com)

Thư viện JavaScript/TypeScript **không chính thức** giúp tương tác với **Zalo Web/PC** bằng cách mô phỏng trình duyệt. Rất phù hợp để xây dựng **chatbot** và gửi tin nhắn hàng loạt (bulk messaging).

## ⚠️ Cảnh báo quan trọng (Đọc trước khi dùng)

- Đây **không phải API chính thức** của Zalo → việc sử dụng có nguy cơ bị **hạn chế** hoặc **khóa tài khoản**.
- Zalo có cơ chế chống spam (rate limit). **Không nên gửi quá nhanh**.
- **Khuyến nghị**: Delay tối thiểu **3–8 giây** giữa các tin nhắn khi gửi hàng loạt.
- Chỉ dùng cho mục đích cá nhân/học tập. Không dùng để spam hoặc thương mại lớn.
- Luôn test trên **tài khoản phụ** trước khi dùng tài khoản chính.
- Tác giả và thư viện **không chịu trách nhiệm** nếu tài khoản của bạn bị ảnh hưởng.

## Tính năng chính

- Đăng nhập bằng QR code (không cần cookie thủ công).
- Lắng nghe tin nhắn thời gian thực (`listener`).
- Gửi tin nhắn văn bản, reply/quote, sticker, hình ảnh, GIF.
- Hỗ trợ cả **tin nhắn cá nhân (User)** và **nhóm (Group)**.
- TypeScript-first (có đầy đủ types).
- Hỗ trợ Bun, Node.js.
- Từ v2.0.0: loại bỏ dependency `sharp` (bạn phải tự cung cấp hàm lấy metadata ảnh nếu cần).

## Cài đặt

```bash
# Khuyến nghị dùng Bun (nhanh và hiệu suất cao)
bun add zca-js

# Hoặc dùng npm
npm install zca-js

Nếu bạn muốn gửi hình ảnh/GIF từ file path, cần cài thêm sharp:
Bashbun add sharp
# hoặc npm install sharp
Migrate từ phiên bản cũ sang V2 (BẮT BUỘC)
Từ v2.0.0, thư viện không còn đi kèm sharp. Bạn phải tự cung cấp hàm imageMetadataGetter khi khởi tạo Zalo.
TypeScriptimport { Zalo } from "zca-js";
import sharp from "sharp";
import fs from "node:fs/promises";

async function imageMetadataGetter(filePath: string) {
  const data = await fs.readFile(filePath);
  const metadata = await sharp(data).metadata();
  return {
    height: metadata.height!,
    width: metadata.width!,
    size: metadata.size || data.length,
  };
}

// Khởi tạo với hàm metadata
const zalo = new Zalo({ imageMetadataGetter });
Nếu bạn không gửi ảnh/GIF từ file, có thể bỏ qua phần này:
TypeScriptconst zalo = new Zalo();
Đăng nhập (Login)
TypeScriptimport { Zalo } from "zca-js";

const zalo = new Zalo({ imageMetadataGetter }); // nếu cần gửi ảnh

// Cách đơn giản nhất (quét QR)
const api = await zalo.loginQR();
console.log("✅ Đăng nhập thành công!");
Thư viện sẽ tự động lưu session để lần sau không cần quét QR lại (kiểm tra thư mục .zca hoặc theo code).
Lắng nghe tin nhắn thời gian thực (Listener)
TypeScriptimport { Zalo, ThreadType } from "zca-js";

const zalo = new Zalo({ imageMetadataGetter });
const api = await zalo.loginQR();

api.listener.on("message", (message) => {
  const isPlainText = typeof message.data.content === "string";

  console.log(`[Tin nhắn mới] Thread: ${message.threadId} | Type: ${message.type}`);

  if (message.isSelf || !isPlainText) return;

  // Xử lý theo loại
  if (message.type === ThreadType.User) {
    console.log("Tin nhắn cá nhân:", message.data.content);
  } else if (message.type === ThreadType.Group) {
    console.log("Tin nhắn nhóm:", message.data.content);
  }
});

2. Đăng nhập bằng Cookie / Session (Không cần quét QR lại)
Thư viện hỗ trợ sử dụng cookie/session đã lưu từ lần đăng nhập trước để login nhanh mà không cần quét QR.
TypeScriptimport { Zalo } from "zca-js";

const zalo = new Zalo({ imageMetadataGetter });

// Cách 1: Sử dụng cookie string (thường lấy từ Zalo Web sau khi đã login)
const api = await zalo.login({
  cookie: "your_full_cookie_string_here",   // Cookie đầy đủ từ Zalo (bao gồm imei, ua, etc.)
});

// Cách 2: Sử dụng session (nếu thư viện đã lưu session trước đó)
const api = await zalo.login({
  session: "session_id_or_saved_session_object",
});

// Cách 3: Kết hợp với qrCallback (nếu cookie/session không hợp lệ sẽ fallback về QR)
const api = await zalo.login({
  cookie: "your_cookie_here",
  qrCallback: (qrCode: string) => {
    console.log("Cookie không hợp lệ hoặc hết hạn. Hãy quét QR sau:");
    console.log(qrCode);
  }
});

console.log("✅ Đăng nhập thành công bằng Cookie/Session!");

// BẮT BUỘC phải start listener
api.listener.start();
console.log("🤖 Bot đang lắng nghe tin nhắn...");
Xây dựng Chatbot (Echo Bot nâng cao)
TypeScriptapi.listener.on("message", async (message) => {
  if (message.isSelf || typeof message.data.content !== "string") return;

  const userMsg = message.data.content.trim().toLowerCase();

  let reply = "Echo: " + message.data.content;

  if (userMsg.includes("chào") || userMsg.includes("hi")) {
    reply = "Chào bạn! Mình là chatbot zca-js đây ❤️";
  } else if (userMsg === "ping") {
    reply = "Pong! 🏓";
  }

  try {
    await api.sendMessage(
      {
        msg: reply,
        quote: message.data,   // Reply lại tin nhắn gốc (rất đẹp)
      },
      message.threadId,
      message.type
    );
    console.log("✅ Đã trả lời thành công");
  } catch (err) {
    console.error("❌ Lỗi khi gửi tin:", err);
  }
});

api.listener.start();
Nâng cấp thành AI Chatbot: Thay phần logic reply bằng gọi OpenAI, Gemini, Grok… (thêm thư viện tương ứng).
Gửi tin nhắn nâng cao
Gửi văn bản + reply
TypeScriptawait api.sendMessage(
  { msg: "Nội dung tin nhắn", quote: message.data },
  threadId,
  threadType
);
Gửi Sticker
TypeScriptconst stickerIds = await api.getStickers("haha");
const sticker = await api.getStickersDetail(stickerIds[0]);

await api.sendMessageSticker(sticker, threadId, threadType);
Gửi hình ảnh / GIF
TypeScriptawait api.sendMessageImage(
  { filePath: "./anh.jpg" },   // hoặc { url: "https://..." }
  threadId,
  threadType
);
Gửi tin nhắn hàng loạt (Bulk Messaging)
Thư viện không có hàm bulk built-in, bạn tự implement với delay.
TypeScriptconst bulkList = [
  { threadId: "1234567890", type: ThreadType.User, msg: "Chào anh A! Đây là tin nhắn thử nghiệm." },
  { threadId: "9876543210", type: ThreadType.User, msg: "Khuyến mãi đặc biệt hôm nay..." },
  // Thêm nhiều hơn...
];

async function delay(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function sendBulk() {
  for (const item of bulkList) {
    try {
      await api.sendMessage({ msg: item.msg }, item.threadId, item.type);
      console.log(`✅ Gửi thành công đến ${item.threadId}`);
    } catch (err) {
      console.error(`❌ Lỗi gửi ${item.threadId}:`, err);
    }

    await delay(5000);   // Delay 5 giây – RẤT QUAN TRỌNG để tránh bị ban
  }
  console.log("🎉 Hoàn thành gửi hàng loạt!");
}

sendBulk();
Mẹo Bulk:

Delay ít nhất 3–8 giây.
Gửi tối đa 50–100 tin/ngày/tài khoản (tùy kinh nghiệm).
Dùng nhiều tài khoản + proxy (xem project MultiZlogin).
Lưu danh sách threadId vào file JSON/Excel.

Best Practices

Luôn kiểm tra message.isSelf để tránh loop vô tận.
Bọc mọi sendMessage trong try/catch.
Chạy bot bằng PM2 hoặc bun --watch.
Log chi tiết để debug.
Không gửi quá nhiều sticker hoặc ảnh liên tục.