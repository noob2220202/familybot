const base = { cwd: __dirname, interpreter: "python3", autorestart: true, max_restarts: 50, restart_delay: 3000, time: true };

module.exports = {
  apps: [
    { ...base, name: "familybot-bot", script: "-m", args: "app.bot" },
    { ...base, name: "familybot-miniapp", script: "-m", args: "app.miniapp" },
    { ...base, name: "familybot-admin", script: "-m", args: "app.admin" },
  ],
};
