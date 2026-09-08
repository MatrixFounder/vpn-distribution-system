// Локализация; словари RU и EN добавляет задача 001.62.
export const LANGUAGES = ["ru", "en"] as const;
export type Language = (typeof LANGUAGES)[number];
