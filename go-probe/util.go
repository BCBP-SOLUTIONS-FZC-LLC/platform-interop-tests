package main

import (
	"encoding/json"
	"os"
)

// printJSON writes v to stdout as a single indented JSON document.
func printJSON(v any) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}
