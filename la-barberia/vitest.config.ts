import { defineConfig } from "vitest/config";
import path from "node:path";

const TEST_DB = process.env.TEST_DATABASE_URL ?? "postgres://postgres@localhost:54329/labarberia_test";

export default defineConfig({
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    globalSetup: ["tests/global-setup.ts"],
    fileParallelism: false,
    testTimeout: 30000,
    env: {
      DATABASE_URL: TEST_DB,
      TEST_DATABASE_URL: TEST_DB,
      APP_SECRET: "test-secret-test-secret-test-secret-0123456789",
      PUBLIC_BASE_URL: "http://localhost:3000",
      RESEND_API_KEY: "",
      EMAIL_FROM: "",
    },
  },
});
