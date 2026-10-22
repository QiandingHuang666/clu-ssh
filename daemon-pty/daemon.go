package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"sync"
	"syscall"

	"github.com/creack/pty"
)

type response struct {
	Ok    bool        `json:"ok"`
	Error string      `json:"error,omitempty"`
	Data  interface{} `json:"data,omitempty"`
}

type session struct {
	d       *daemon
	name    string
	cmd     *exec.Cmd
	master  *os.File
	logFile *os.File
	mu      sync.Mutex
	client  net.Conn
}

type daemon struct {
	mu       sync.Mutex
	sessions map[string]*session
	dir      string // ~/.clu/ptyd（日志目录）
	sockPath string
}

var nameRe = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,64}$`)

func validName(name string) bool { return nameRe.MatchString(name) }

func writeJSON(conn net.Conn, resp response) {
	_ = json.NewEncoder(conn).Encode(resp)
}

// serve：守护入口（信号触发优雅退出）。
func serve() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	d := &daemon{
		sessions: map[string]*session{},
		dir:      filepath.Join(home, ".clu", "ptyd"),
		sockPath: filepath.Join(home, ".clu", "ptyd.sock"),
	}
	if err := os.MkdirAll(d.dir, 0o700); err != nil {
		return err
	}
	ln, err := net.Listen("unix", d.sockPath)
	if err != nil {
		return fmt.Errorf("daemon 已在运行？%w", err)
	}
	_ = os.Chmod(d.sockPath, 0o600)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	go func() {
		<-ctx.Done()
		d.stopAll()
		_ = os.Remove(d.sockPath)
		os.Exit(0)
	}()
	for {
		conn, err := ln.Accept()
		if err != nil {
			return err
		}
		go d.handle(conn)
	}
}

// acceptLoop：供测试直接驱动。
func (d *daemon) acceptLoop(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		go d.handle(conn)
	}
}

func (d *daemon) handle(conn net.Conn) {
	defer conn.Close()
	line, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil {
		return
	}
	var req request
	if err := json.Unmarshal(line, &req); err != nil {
		writeJSON(conn, response{Error: "bad request"})
		return
	}
	switch req.Op {
	case "create":
		d.create(conn, req)
	case "attach":
		d.attach(conn, req) // 阻塞直到客户端断开
	case "list":
		writeJSON(conn, response{Ok: true, Data: d.snapshot()})
	case "kill":
		d.kill(conn, req.Name)
	case "stop":
		d.stopAll()
		_ = os.Remove(d.sockPath)
		os.Exit(0)
	default:
		writeJSON(conn, response{Error: "unknown op"})
	}
}

func (d *daemon) snapshot() []map[string]interface{} {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]map[string]interface{}, 0, len(d.sessions))
	for name, s := range d.sessions {
		s.mu.Lock()
		attached := s.client != nil
		s.mu.Unlock()
		out = append(out, map[string]interface{}{
			"name": name, "attached": attached,
			"log": filepath.Join(d.dir, name+".log"),
		})
	}
	return out
}

func (d *daemon) create(conn net.Conn, req request) {
	if !validName(req.Name) {
		writeJSON(conn, response{Error: "非法会话名"})
		return
	}
	d.mu.Lock()
	_, exists := d.sessions[req.Name]
	d.mu.Unlock()
	if exists { // 幂等：已存在视为成功（clu 层有 exists 前置守卫）
		writeJSON(conn, response{Ok: true})
		return
	}
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/bash"
	}
	cmd := exec.Command(shell)
	cmd.Env = append(os.Environ(), "CLUPY_SESSION="+req.Name)
	rows, cols := req.Rows, req.Cols
	if rows <= 0 {
		rows = 24
	}
	if cols <= 0 {
		cols = 80
	}
	tty, err := pty.StartWithSize(cmd, &pty.Winsize{Rows: uint16(rows), Cols: uint16(cols)})
	if err != nil {
		writeJSON(conn, response{Error: err.Error()})
		return
	}
	logFile, err := os.OpenFile(filepath.Join(d.dir, req.Name+".log"),
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		_ = cmd.Process.Kill()
		writeJSON(conn, response{Error: err.Error()})
		return
	}
	s := &session{d: d, name: req.Name, cmd: cmd, master: tty, logFile: logFile}
	d.mu.Lock()
	d.sessions[req.Name] = s
	d.mu.Unlock()
	go s.pump()
	go func() { _ = cmd.Wait() }() // 僵尸回收；EOF 由 master 关闭触发
	writeJSON(conn, response{Ok: true})
}

func (d *daemon) attach(conn net.Conn, req request) {
	d.mu.Lock()
	s := d.sessions[req.Name]
	d.mu.Unlock()
	if s == nil {
		writeJSON(conn, response{Error: "no such session"})
		return
	}
	s.mu.Lock()
	if s.client != nil {
		s.mu.Unlock()
		writeJSON(conn, response{Error: "session busy（已有客户端接入）"})
		return
	}
	s.client = conn
	s.mu.Unlock()
	if req.Rows > 0 && req.Cols > 0 {
		_ = pty.Setsize(s.master, &pty.Winsize{Rows: uint16(req.Rows), Cols: uint16(req.Cols)})
	}
	writeJSON(conn, response{Ok: true, Data: filepath.Join(d.dir, s.name+".log")})
	// 客户端 -> pty（断开即脱离，会话照活）
	_, _ = io.Copy(s.master, conn)
	s.mu.Lock()
	if s.client == conn {
		s.client = nil
	}
	s.mu.Unlock()
}

func (d *daemon) kill(conn net.Conn, name string) {
	d.mu.Lock()
	s := d.sessions[name]
	delete(d.sessions, name)
	d.mu.Unlock()
	if s == nil {
		writeJSON(conn, response{Error: "no such session"})
		return
	}
	s.mu.Lock()
	if s.client != nil {
		_ = s.client.Close()
	}
	s.mu.Unlock()
	if s.cmd.Process != nil {
		_ = s.cmd.Process.Kill()
	}
	_ = s.master.Close()
	_ = s.logFile.Close()
	writeJSON(conn, response{Ok: true})
}

func (d *daemon) stopAll() {
	d.mu.Lock()
	sessions := make([]*session, 0, len(d.sessions))
	for _, s := range d.sessions {
		sessions = append(sessions, s)
	}
	d.sessions = map[string]*session{}
	d.mu.Unlock()
	for _, s := range sessions {
		if s.cmd.Process != nil {
			_ = s.cmd.Process.Kill()
		}
		s.mu.Lock()
		if s.client != nil {
			_ = s.client.Close()
		}
		s.mu.Unlock()
		_ = s.master.Close()
		_ = s.logFile.Close()
	}
}

// pump：pty -> 日志 + 在线客户端；会话结束时自清理。
func (s *session) pump() {
	d := s.d
	buf := make([]byte, 32*1024)
	for {
		n, err := s.master.Read(buf)
		if n > 0 {
			_, _ = s.logFile.Write(buf[:n])
			s.mu.Lock()
			c := s.client
			s.mu.Unlock()
			if c != nil {
				_, _ = c.Write(buf[:n])
			}
		}
		if err != nil {
			break
		}
	}
	d.mu.Lock()
	delete(d.sessions, s.name)
	d.mu.Unlock()
	s.mu.Lock()
	c := s.client
	s.client = nil
	s.mu.Unlock()
	if c != nil {
		_ = c.Close() // 让 attacher 看到 EOF
	}
	_ = s.logFile.Close()
}
