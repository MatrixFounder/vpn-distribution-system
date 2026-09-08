package app

import "errors"

// errEmptyConfigPath — путь к конфигурации не задан.
var errEmptyConfigPath = errors.New("путь к файлу конфигурации не задан")
