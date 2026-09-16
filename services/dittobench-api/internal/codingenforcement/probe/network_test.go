//go:build linux

package probe

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

const fakeRunner = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func testAgent() *NetAgent {
	return &NetAgent{RunnerSHA256: func() (string, error) { return fakeRunner, nil }}
}

func loopbackPort(t *testing.T, listener net.Listener) int {
	t.Helper()
	_, port, err := net.SplitHostPort(listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	value, _ := strconv.Atoi(port)
	return value
}

func TestClassifyDialErrorMapsKernelRefusalsAndNeverDefaultsToADenial(t *testing.T) {
	cases := map[string]error{
		OutcomeConnected:    nil,
		OutcomeRefused:      &net.OpError{Op: "dial", Err: os.NewSyscallError("connect", syscall.ECONNREFUSED)},
		OutcomeReset:        syscall.ECONNRESET,
		OutcomeNotPermitted: os.NewSyscallError("connect", syscall.EPERM),
		OutcomeUnreachable:  os.NewSyscallError("connect", syscall.EHOSTUNREACH),
		OutcomeTimeout:      context.DeadlineExceeded,
	}
	for want, err := range cases {
		if got := ClassifyDialError(err); got != want {
			t.Errorf("%v classified %s, want %s", err, got, want)
		}
	}
	for _, err := range []error{errors.New("tls: bad certificate"), io.ErrUnexpectedEOF, syscall.EINVAL} {
		if got := ClassifyDialError(err); got != OutcomeProbeError {
			t.Errorf("unknown %v classified %s", err, got)
		}
	}
	if classifyEstablished(0, os.ErrDeadlineExceeded) != OutcomeConnected {
		t.Error("an idle live flow must not look cut")
	}
	if classifyEstablished(0, io.EOF) != OutcomeClosed || classifyEstablished(0, syscall.ETIMEDOUT) != OutcomeTimeout {
		t.Error("established flow classification differs")
	}
}

func TestDecodeNetRequestIsClosedPerOp(t *testing.T) {
	good := []string{
		`{"op":"hello"}`,
		`{"op":"connect","address":"10.20.0.7","port":5432,"timeout_ms":1000}`,
		`{"op":"connect","address":"2606:4700:4700::1111","port":443,"timeout_ms":1000}`,
		`{"op":"proxy_forward","address":"10.30.0.5","port":18090,"target":"example.com:443","timeout_ms":1000}`,
		`{"op":"dns","target":"example.com","timeout_ms":1000}`,
		`{"op":"listen","address":"10.30.0.4","port":18080}`,
		`{"op":"accept","listener":"l1","timeout_ms":1000}`,
		`{"op":"check","connection":"c2","timeout_ms":1000}`,
	}
	for _, line := range good {
		if _, err := DecodeNetRequest([]byte(line)); err != nil {
			t.Errorf("%s refused: %v", line, err)
		}
	}
	bad := []string{
		`{"op":"hello","address":"10.0.0.1"}`,
		`{"op":"connect","address":"example.com","port":443,"timeout_ms":1000}`,
		`{"op":"connect","address":"10.20.0.7","port":5432}`,
		`{"op":"connect","address":"10.20.0.7","port":5432,"timeout_ms":1000,"payload":"x"}`,
		`{"op":"handshake","address":"10.20.0.7","port":5432,"timeout_ms":1000,"target":"a.b:1"}`,
		`{"op":"proxy_forward","address":"10.30.0.5","port":18090,"target":"http://x","timeout_ms":1000}`,
		`{"op":"dns","target":"EXAMPLE.com","timeout_ms":1000}`,
		`{"op":"listen","address":"::1","port":18080}`,
		`{"op":"accept","listener":"c1","timeout_ms":1000}`,
		`{"op":"write","address":"10.20.0.7","port":5432,"timeout_ms":1000}`,
		`{"op":"hello"} {"op":"exit"}`,
	}
	for _, line := range bad {
		if _, err := DecodeNetRequest([]byte(line)); err == nil {
			t.Errorf("%s accepted", line)
		}
	}
}

func TestHandshakeAndConnectWriteNoPayload(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	received := make(chan int, 2)
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
			body, _ := io.ReadAll(conn)
			received <- len(body)
			_ = conn.Close()
		}
	}()
	port := loopbackPort(t, listener)
	agent := testAgent()
	for op, want := range map[string]string{"handshake": OutcomeHandshakeCompleted, "connect": OutcomeConnected} {
		response := agent.Handle(context.Background(), NetRequest{Op: op, Address: "127.0.0.1", Port: port, TimeoutMS: 2000})
		if response.Outcome != want || response.Error != "" {
			t.Fatalf("%s = %+v", op, response)
		}
		if n := <-received; n != 0 {
			t.Fatalf("%s wrote %d payload bytes to the endpoint", op, n)
		}
	}
	_ = listener.Close()
	refused := agent.Handle(context.Background(), NetRequest{Op: "connect", Address: "127.0.0.1", Port: port, TimeoutMS: 2000})
	failed := agent.Handle(context.Background(), NetRequest{Op: "handshake", Address: "127.0.0.1", Port: port, TimeoutMS: 2000})
	if refused.Outcome != OutcomeRefused || failed.Outcome != OutcomeHandshakeFailed {
		t.Fatalf("closed port = %+v / %+v", refused, failed)
	}
}

func TestInjectedKernelRefusalsReachTheReport(t *testing.T) {
	for errno, want := range map[syscall.Errno]string{
		syscall.EPERM: OutcomeNotPermitted, syscall.EHOSTUNREACH: OutcomeUnreachable, syscall.ECONNRESET: OutcomeReset,
	} {
		agent := testAgent()
		agent.Dial = func(context.Context, string, string) (net.Conn, error) {
			return nil, &net.OpError{Op: "dial", Err: os.NewSyscallError("connect", errno)}
		}
		response := agent.Handle(context.Background(), NetRequest{Op: "connect", Address: "1.1.1.1", Port: 443, TimeoutMS: 100})
		if response.Outcome != want {
			t.Errorf("%v = %s, want %s", errno, response.Outcome, want)
		}
	}
}

func proxyServer(t *testing.T, status string) (int, chan string) {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	lines := make(chan string, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		line, _ := bufio.NewReader(conn).ReadString('\n')
		lines <- line
		_, _ = io.WriteString(conn, status+"\r\nContent-Length: 0\r\n\r\n")
	}()
	return loopbackPort(t, listener), lines
}

func TestProxyForwardDistinguishesRefusalFromForwarding(t *testing.T) {
	for status, want := range map[string]string{
		"HTTP/1.1 403 Forbidden":                 OutcomeProxyRefused,
		"HTTP/1.1 405 Method Not Allowed":        OutcomeProxyRefused,
		"HTTP/1.1 200 Connection established":    OutcomeProxyForwarded,
		"HTTP/1.1 502 Bad Gateway":               OutcomeProbeError,
		"SSH-2.0-OpenSSH_9.6 not an http server": OutcomeProbeError,
	} {
		port, lines := proxyServer(t, status)
		response := testAgent().Handle(context.Background(), NetRequest{
			Op: "proxy_forward", Address: "127.0.0.1", Port: port, Target: "example.com:443", TimeoutMS: 2000,
		})
		if response.Outcome != want {
			t.Errorf("%q = %s, want %s", status, response.Outcome, want)
		}
		if line := <-lines; line != "CONNECT example.com:443 HTTP/1.1\r\n" {
			t.Errorf("request line = %q", line)
		}
	}
}

func TestDNSQueryClassifiesAnswersAndRefusals(t *testing.T) {
	resolv := filepath.Join(t.TempDir(), "resolv.conf")
	if err := os.WriteFile(resolv, []byte("search invalid\nnameserver 127.0.0.11\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, answer := range map[string]struct {
		flags   uint16
		answers uint16
		want    string
	}{
		"resolved": {0x8180, 1, OutcomeConnected},
		"servfail": {0x8182, 0, OutcomeRefused},
		"empty":    {0x8180, 0, OutcomeRefused},
	} {
		server, err := net.ListenPacket("udp4", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		go func() {
			buffer := make([]byte, 512)
			n, peer, err := server.ReadFrom(buffer)
			if err != nil || n < 12 {
				return
			}
			reply := append([]byte(nil), buffer[:n]...)
			reply[2], reply[3] = byte(answer.flags>>8), byte(answer.flags)
			reply[6], reply[7] = byte(answer.answers>>8), byte(answer.answers)
			_, _ = server.WriteTo(reply, peer)
		}()
		agent := testAgent()
		agent.ResolvConf = resolv
		var dialed string
		agent.Dial = func(ctx context.Context, network, address string) (net.Conn, error) {
			dialed = network + " " + address
			var dialer net.Dialer
			return dialer.DialContext(ctx, network, server.LocalAddr().String())
		}
		response := agent.Handle(context.Background(), NetRequest{Op: "dns", Target: "example.com", TimeoutMS: 2000})
		_ = server.Close()
		if response.Outcome != answer.want || dialed != "udp 127.0.0.11:53" {
			t.Errorf("%s = %s via %q", name, response.Outcome, dialed)
		}
	}
	question := EncodeDNSQuestion(7, "example.com")
	if !bytes.HasSuffix(question, []byte("\x07example\x03com\x00\x00\x01\x00\x01")) {
		t.Fatalf("question = %x", question)
	}
	if ClassifyDNSResponse(7, question) != OutcomeProbeError {
		t.Fatal("a query echoed back is not an answer")
	}
}

func TestListenAcceptEstablishAndCheckWithoutPayload(t *testing.T) {
	agent := testAgent()
	defer agent.Close()
	if refused := agent.Handle(context.Background(), NetRequest{Op: "listen", Address: "127.0.0.1", Port: 0}); refused.Error == "" {
		t.Fatalf("listen on port 0 = %+v", refused)
	}
	// The protocol refuses port 0; the test binds an ephemeral port directly.
	listen := agent.listen(NetResponse{Schema: NetAgentSchema, Op: "listen"}, "127.0.0.1:0")
	if listen.Outcome != OutcomeAccepted || listen.Listener == "" {
		t.Fatalf("listen = %+v", listen)
	}
	agent.mu.Lock()
	port := loopbackPort(t, agent.listeners[listen.Listener].listener)
	agent.mu.Unlock()
	empty := agent.Handle(context.Background(), NetRequest{Op: "accept", Listener: listen.Listener, TimeoutMS: 50})
	if empty.Outcome != OutcomeTimeout || empty.RemoteAddress != "" {
		t.Fatalf("no connection = %+v", empty)
	}
	client := testAgent()
	defer client.Close()
	established := client.Handle(context.Background(), NetRequest{Op: "establish", Address: "127.0.0.1", Port: port, TimeoutMS: 2000})
	if established.Outcome != OutcomeConnected || established.Connection == "" {
		t.Fatalf("establish = %+v", established)
	}
	accepted := agent.Handle(context.Background(), NetRequest{Op: "accept", Listener: listen.Listener, TimeoutMS: 2000})
	if accepted.Outcome != OutcomeAccepted || accepted.RemoteAddress != "127.0.0.1" || accepted.Connection == "" {
		t.Fatalf("accept = %+v", accepted)
	}
	alive := client.Handle(context.Background(), NetRequest{Op: "check", Connection: established.Connection, TimeoutMS: 300})
	if alive.Outcome != OutcomeConnected {
		t.Fatalf("an idle live flow = %+v", alive)
	}
	agent.mu.Lock()
	_ = agent.connections[accepted.Connection].Close()
	agent.mu.Unlock()
	closed := client.Handle(context.Background(), NetRequest{Op: "check", Connection: established.Connection, TimeoutMS: 2000})
	if closed.Outcome != OutcomeClosed {
		t.Fatalf("a flow the peer closed = %+v", closed)
	}
	unknown := client.Handle(context.Background(), NetRequest{Op: "check", Connection: "c999", TimeoutMS: 10})
	if unknown.Outcome != OutcomeProbeError {
		t.Fatalf("unknown connection = %+v", unknown)
	}
}

func TestServeNetAgentAnswersEachLineAndStopsAtExit(t *testing.T) {
	input := strings.NewReader(strings.Join([]string{
		`{"op":"hello"}`,
		`{"op":"connect","address":"not-an-ip","port":1,"timeout_ms":10}`,
		`{"op":"exit"}`,
		`{"op":"hello"}`,
	}, "\n") + "\n")
	var output bytes.Buffer
	if err := ServeNetAgent(context.Background(), testAgent(), input, &output); err != nil {
		t.Fatal(err)
	}
	var responses []NetResponse
	decoder := json.NewDecoder(&output)
	for decoder.More() {
		var response NetResponse
		if err := decoder.Decode(&response); err != nil {
			t.Fatal(err)
		}
		responses = append(responses, response)
	}
	if len(responses) != 3 {
		t.Fatalf("responses = %+v", responses)
	}
	hello := responses[0]
	if hello.Schema != NetAgentSchema || hello.ProbeRunnerBinarySHA256 != fakeRunner || hello.PID != os.Getpid() || hello.UID == nil || *hello.UID != os.Getuid() {
		t.Fatalf("hello = %+v", hello)
	}
	if responses[1].Error == "" || responses[1].Outcome != "" {
		t.Fatalf("malformed request = %+v", responses[1])
	}
	if responses[2].Op != "exit" {
		t.Fatalf("exit = %+v", responses[2])
	}
}

func TestNetPlanAllowsOnlyOneShotAttempts(t *testing.T) {
	plan := fmt.Sprintf(`{"schema":%q,"requests":[{"op":"handshake","address":"127.0.0.1","port":9,"timeout_ms":200}]}`, NetPlanSchema)
	decoded, err := DecodeNetPlan([]byte(plan))
	if err != nil {
		t.Fatal(err)
	}
	report, err := RunNetPlan(context.Background(), testAgent(), decoded)
	if err != nil || report.Schema != NetReportSchema || len(report.Responses) != 1 || report.Responses[0].Outcome != OutcomeHandshakeFailed {
		t.Fatalf("report = %+v, %v", report, err)
	}
	for _, bad := range []string{
		fmt.Sprintf(`{"schema":%q,"requests":[{"op":"listen","address":"127.0.0.1","port":9}]}`, NetPlanSchema),
		fmt.Sprintf(`{"schema":%q,"requests":[]}`, NetPlanSchema),
		`{"schema":"dittobench-coding-native-enforcement-evidence-v1","requests":[{"op":"connect","address":"127.0.0.1","port":9,"timeout_ms":1}]}`,
		fmt.Sprintf(`{"schema":%q,"requests":[{"op":"connect","address":"127.0.0.1","port":9,"timeout_ms":1}],"matched":true}`, NetPlanSchema),
	} {
		if _, err := DecodeNetPlan([]byte(bad)); err == nil {
			t.Errorf("%s accepted", bad)
		}
	}
	failing := &NetAgent{RunnerSHA256: func() (string, error) { return "", errors.New("unmeasured") }}
	if _, err := RunNetPlan(context.Background(), failing, decoded); err == nil {
		t.Fatal("an unmeasured runner produced a report")
	}
}
