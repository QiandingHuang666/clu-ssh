package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func startTestDaemon(t *testing.T) *daemon {
	t.Helper()
	home := filepath.Join(t.TempDir(), "home")
	if err := os.MkdirAll(home, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", home)
	t.Setenv("SHELL", "/bin/bash")
	d := &daemon{
		sessions: map[string]*session{},
		dir:      filepath.Join(home, ".clu", "ptyd"),
		// macOS sun_path 上限 104：socket 用短路径，日志仍放临时目录
		sockPath: filepath.Join("/tmp", fmt.Sprintf("clu-ptyd-test-%d.sock", time.Now().UnixNano())),
	}
	if err := os.MkdirAll(d.dir, 0o700); err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("unix", d.sockPath)
	if err != nil {
		t.Fatal(err)
	}
	go d.acceptLoop(ln)
	t.Cleanup(func() {
		d.stopAll()
		_ = ln.Close()
	})
	return d
}

func dialReq(t *testing.T, sock string, req request) (response, net.Conn) {
	t.Helper()
	conn, err := net.Dial("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	if err := json.NewEncoder(conn).Encode(req); err != nil {
		t.Fatal(err)
	}
	raw, err := readLine(conn)
	if err != nil {
		t.Fatal(err)
	}
	var resp response
	if err := json.Unmarshal(raw, &resp); err != nil {
		t.Fatal(err)
	}
	return resp, conn
}

func TestCreateListKill(t *testing.T) {
	d := startTestDaemon(t)
	resp, _ := dialReq(t, d.sockPath, request{Op: "create", Name: "s1"})
	if !resp.Ok {
		t.Fatalf("create failed: %s", resp.Error)
	}
	resp, _ = dialReq(t, d.sockPath, request{Op: "list"})
	data, _ := json.Marshal(resp.Data)
	if !bytes.Contains(data, []byte("s1")) {
		t.Fatalf("list missing s1: %s", data)
	}
	resp, _ = dialReq(t, d.sockPath, request{Op: "kill", Name: "s1"})
	if !resp.Ok {
		t.Fatalf("kill failed: %s", resp.Error)
	}
	resp, _ = dialReq(t, d.sockPath, request{Op: "list"})
	data, _ = json.Marshal(resp.Data)
	if bytes.Contains(data, []byte("s1")) {
		t.Fatalf("s1 should be gone: %s", data)
	}
}

func TestCreateRejectsBadName(t *testing.T) {
	d := startTestDaemon(t)
	resp, _ := dialReq(t, d.sockPath, request{Op: "create", Name: "../evil"})
	if resp.Ok || resp.Error == "" {
		t.Fatalf("bad name should be rejected: %+v", resp)
	}
}

func TestCreateIdempotent(t *testing.T) {
	d := startTestDaemon(t)
	for i := 0; i < 2; i++ {
		resp, _ := dialReq(t, d.sockPath, request{Op: "create", Name: "dup"})
		if !resp.Ok {
			t.Fatalf("create #%d failed: %s", i+1, resp.Error)
		}
	}
	d.mu.Lock()
	n := len(d.sessions)
	d.mu.Unlock()
	if n != 1 {
		t.Fatalf("expected 1 session, got %d", n)
	}
}

func TestAttachEchoAndKeepalive(t *testing.T) {
	d := startTestDaemon(t)
	if _, conn := dialReq(t, d.sockPath, request{Op: "create", Name: "echo"}); false {
		_ = conn
	}
	resp, _ := dialReq(t, d.sockPath, request{Op: "create", Name: "echo"})
	if !resp.Ok {
		t.Fatalf("create failed: %s", resp.Error)
	}
	// 接入
	conn, err := net.Dial("unix", d.sockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := json.NewEncoder(conn).Encode(request{Op: "attach", Name: "echo", Rows: 24, Cols: 80}); err != nil {
		t.Fatal(err)
	}
	raw, err := readLine(conn)
	if err != nil {
		t.Fatal(err)
	}
	var resp2 response
	if err := json.Unmarshal(raw, &resp2); err != nil {
		t.Fatal(err)
	}
	if !resp2.Ok {
		t.Fatalf("attach failed: %s", resp2.Error)
	}
	// 断开 attach：会话必须仍然存活（保活本体）
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	resp, _ = dialReq(t, d.sockPath, request{Op: "list"})
	data, _ := json.Marshal(resp.Data)
	if !bytes.Contains(data, []byte("echo")) {
		t.Fatalf("session should survive detach: %s", data)
	}
	// 再次 attach：敲 echo 命令并期待回显输出
	conn2, err := net.Dial("unix", d.sockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer conn2.Close()
	if err := json.NewEncoder(conn2).Encode(request{Op: "attach", Name: "echo", Rows: 24, Cols: 80}); err != nil {
		t.Fatal(err)
	}
	if _, err = readLine(conn2); err != nil {
		t.Fatal(err)
	}
	if _, err := conn2.Write([]byte("echo marker-abc-123\r")); err != nil {
		t.Fatal(err)
	}
	_ = conn2.SetReadDeadline(time.Now().Add(5 * time.Second))
	var buf bytes.Buffer
	tick := time.Now()
	for time.Since(tick) < 5*time.Second && !bytes.Contains(buf.Bytes(), []byte("marker-abc-123")) {
		chunk := make([]byte, 4096)
		n, err := conn2.Read(chunk)
		if n > 0 {
			buf.Write(chunk[:n])
		}
		if err != nil {
			break
		}
	}
	if !bytes.Contains(buf.Bytes(), []byte("marker-abc-123")) {
		t.Fatalf("expected command output, got: %q", buf.String())
	}
}
