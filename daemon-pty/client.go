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
	// 回放日志尾部（提示符与近期输出在 detach 期间只进日志，不回放则 attach 见不到提示符）
	if logPath, ok := resp.Data.(string); ok && logPath != "" {
		replayTail(os.Stdout, logPath, 8192)
	}
	fmt.Fprintln(os.Stderr, "[clu] 已 attach。detach = 断开本连接（ssh 转义 ~. 或关窗口），会话继续；exit = 结束会话。")
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


// replayTail：把文件尾部至多 maxBytes 字节写到 w（attach 回放提示符与近期输出）。
func replayTail(w io.Writer, path string, maxBytes int64) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	st, err := f.Stat()
	if err == nil && st.Size() > maxBytes {
		_, _ = f.Seek(-maxBytes, io.SeekEnd)
	}
	_, _ = io.Copy(w, f)
}
