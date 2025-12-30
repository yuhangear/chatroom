// 后端服务器配置
// 这个文件会在 index.html 中加载，用于配置后端服务器地址
// 部署到 GitHub Pages 时，可以修改这个文件来设置你的服务器地址

window.APP_CONFIG = {
  // 后端服务器地址（IPv6 地址或域名）
  // 示例: '2607:8700:5500:63c9::2' 或 'your-server.com'
  serverIp: '2607:8700:5500:63c9::2',
  
  // WebSocket 端口
  wsPort: 38080,
  
  // 是否使用 HTTPS/WSS（生产环境必须为 true，因为 WebRTC 要求安全连接）
  useSecure: true
};


