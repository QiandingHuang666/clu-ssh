package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
)

func sockPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".clu", "ptyd.sock"), nil
}

// clientOp：控制面操作（create/list/kill/stop），一行 JSON 应答。
func clientOp(req request) error {
	sock, err := sockPath()
	if err != nil {
		return err
	}
	conn, err := net.Dial("unix", sock)
	if err != nil {
		return fmt.Errorf("daemon 未运行（%w）；先启动：clu-ptyd serve", err)
	}
	defer conn.Close()
	if err := json.NewEncoder(conn).Encode(req); err != nil {
		return err
	}
	var resp response
	if err := json.NewDecoder(conn).Decode(&resp); err != nil {
		return err
	}
	if !resp.Ok {
		return fmt.Errorf("%s", resp.Error)
	}
	if req.Op == "list" {
		out, _ := json.MarshalIndent(resp.Data, "", "  ")
		fmt.Println(string(out))
		return nil
	}
	fmt.Println("ok")
	return nil
}

// runAttach：接入会话。stdin（ssh -t 提供的远端 pty）切 raw，双向桥接。
func runAttach(req request) error {
	sock, err := sockPath()
	if err != nil {
		return err
	}
	conn, err := net.Dial("unix", sock)
	if err != nil {
		return fmt.Errorf("daemon 未运行（%w）", err)
	}
	defer conn.Close()
	if err := json.NewEncoder(conn).Encode(req); err != nil {
		return err
	}
	// 逐字节读应答行，避免 bufio 缓冲吃掉紧随其后的回放数据
	respRaw, err := readLine(conn)
	if err != nil {
		return err
	}
	var resp response
	if err := json.Unmarshal(respRaw, &resp); err != nil {
		return err
	}
	if !resp.Ok {
		return fmt.Errorf("%s", resp.Error)
	}
	fd := int(os.Stdin.Fd())
	if isTerminal(fd) {
		old, err := makeRaw(fd)
		if err != nil {
			return err
		}
		defer restore(fd, old)
	}
	done := make(chan error, 2)
	go func() { _, err := io.Copy(conn, os.Stdin); done <- err }()
	go func() { _, err := io.Copy(os.Stdout, conn); done <- err }()
	return <-done
}
