const base = {
  cwd: __dirname,
  script: "python3",
  interpreter: "none",
  env: { PYTHONUNBUFFERED: "1" },
  autorestart: true,
  exp_backoff_restart_delay: 3000,
  time: true,
};

module.exports = {
  apps: [
    { ...base, name: "familybot-bot", args: "-m app.bot" },
    { ...base, name: "familybot-miniapp", args: "-m app.miniapp" },
    { ...base, name: "familybot-admin", args: "-m app.admin" },
  ],
};
