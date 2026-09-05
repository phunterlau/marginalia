import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  use: {
    baseURL: process.env.REVIEW_URL || "http://127.0.0.1:8765",
    channel: "chrome",
  },
  workers: 1,
});
