// clu-ptyd：clu 的远端 pty 会话守护（无 tmux 机器上的可交互保活后端）。
//
// 子命令：
//
//	serve                          守护进程（unix socket ~/.clu/ptyd.sock）
//	create -name N [-rows R -cols C]  创建交互会话（bash -l）
//	attach -name N [-rows R -cols C]  接入会话（裸字节桥接；本地 raw 由 ssh -t 处理）
//	list                           会话列表（JSON）
//	kill   -name N                 结束会话
//	stop                           结束所有会话并退出守护
//	version                        版本
package main

import (
	"flag"
	"fmt"
	"os"
)

var version = "dev"

type request struct {
	Op   string `json:"op"`
	Name string `json:"name"`
	Rows int    `json:"rows"`
	Cols int    `json:"cols"`
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "serve":
		err = serve()
	case "create":
		fs := flag.NewFlagSet("create", flag.ExitOnError)
		name := fs.String("name", "", "session name")
		rows := fs.Int("rows", 24, "rows")
		cols := fs.Int("cols", 80, "cols")
		_ = fs.Parse(os.Args[2:])
		err = clientOp(request{Op: "create", Name: *name, Rows: *rows, Cols: *cols})
	case "attach":
		fs := flag.NewFlagSet("attach", flag.ExitOnError)
		name := fs.String("name", "", "session name")
		rows := fs.Int("rows", 24, "rows")
		cols := fs.Int("cols", 80, "cols")
		_ = fs.Parse(os.Args[2:])
		err = runAttach(request{Op: "attach", Name: *name, Rows: *rows, Cols: *cols})
	case "list":
		err = clientOp(request{Op: "list"})
	case "kill":
		fs := flag.NewFlagSet("kill", flag.ExitOnError)
		name := fs.String("name", "", "session name")
		_ = fs.Parse(os.Args[2:])
		err = clientOp(request{Op: "kill", Name: *name})
	case "stop":
		err = clientOp(request{Op: "stop"})
	case "version":
		fmt.Println("clu-ptyd", version)
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "clu-ptyd:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage: clu-ptyd <serve|create|attach|list|kill|stop|version>`)
}
