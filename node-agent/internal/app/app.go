// Package app — точка входа агента: конфигурация и запуск цикла работы.
// Цикл enroll → sync → reports добавляется задачей 001.53 (docs/PLAN.md); сигнатура Run
// совпадает с контрактом этой задачи.
package app

import "context"

// Version — версия агента; подставляется при сборке через -ldflags "-X node-agent/internal/app.Version=…".
var Version = "0.0.0-dev"

// Config — параметры запуска агента. Поля добавляет задача 001.53 (internal/config).
type Config struct {
	// ConfigPath — путь к файлу конфигурации агента.
	ConfigPath string
}

// Run запускает агент и блокируется до отмены ctx.
// В каркасе задачи 001.01 цикл пуст: функция ждёт отмены контекста и завершается без ошибки.
func Run(ctx context.Context, cfg Config) error {
	if cfg.ConfigPath == "" {
		return errEmptyConfigPath
	}
	<-ctx.Done()
	return nil
}
