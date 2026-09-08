// Command node-agent — агент VPN-ноды: регистрация, синхронизация состояния с Control Plane,
// учёт трафика и управление Xray. Компонент C-05 в docs/ARCHITECTURE.md.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"node-agent/internal/app"
)

func main() {
	os.Exit(run())
}

// run разбирает флаги и запускает агент; код возврата отдаётся в main, чтобы defer отработал.
func run() int {
	configPath := flag.String("config", "/etc/node-agent/config.yaml", "путь к файлу конфигурации")
	showVersion := flag.Bool("version", false, "вывести версию и выйти")
	flag.Parse()

	if *showVersion {
		fmt.Println(app.Version)
		return 0
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := app.Run(ctx, app.Config{ConfigPath: *configPath}); err != nil {
		fmt.Fprintln(os.Stderr, "node-agent:", err)
		return 1
	}
	return 0
}
