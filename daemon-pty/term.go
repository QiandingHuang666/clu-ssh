package main

import (
	"net"

	"golang.org/x/term"
)

func isTerminal(fd int) bool { return term.IsTerminal(fd) }

func makeRaw(fd int) (*term.State, error) { return term.MakeRaw(fd) }

func restore(fd int, state *term.State) { _ = term.Restore(fd, state) }

// readLine：逐字节读到 '\n'（attach 场景避免 bufio 缓冲吃掉回放流）。
func readLine(conn net.Conn) ([]byte, error) {
	line := make([]byte, 0, 128)
	buf := make([]byte, 1)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			if buf[0] == '\n' {
				return line, nil
			}
			line = append(line, buf[0])
		}
		if err != nil {
			if len(line) > 0 {
				return line, nil
			}
			return nil, err
		}
	}
}
