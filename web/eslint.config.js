// Плоская конфигурация ESLint 9 для всех рабочих пространств.
import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["**/dist/**", "**/node_modules/**", "**/*.tsbuildinfo"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { files: ["**/*.ts", "**/*.tsx"], rules: {} },
);
