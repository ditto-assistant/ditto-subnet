package probe

// Network observation agent (B5 PR4).
//
// The agent performs single network attempts for the root network collector
// (infra/scripts/collect-coding-native-enforcement.py) and reports catalog
// outcome names. It runs in three positions the collector places it in: the
// candidate container (as the candidate identity), the worker cgroup
// (system.slice/ditto-coding-hosted-worker.service) and the daemon user's
// cgroup. It never assembles an evidence record, never decides whether an
// outcome matches, and reads nothing but its own requests.
//
// Positive checks are handshake-only: a TCP connection is opened and closed
// without writing a byte. Only the refusing proxy (a CONNECT request line) and
// the candidate's own resolver (one DNS question) ever receive bytes, and
// neither is a trusted endpoint.

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Agent schemas. Neither is an evidence record, and the offline verifier
// refuses both as one.
const (
	NetAgentSchema  = "dittobench-coding-native-net-agent-v1"
	NetPlanSchema   = "dittobench-coding-native-net-plan-v1"
	NetReportSchema = "dittobench-coding-native-net-report-v1"
)

// Catalog outcome names the agent can report.
const (
	OutcomeConnected          = "connected"
	OutcomeRefused            = "refused"
	OutcomeReset              = "reset"
	OutcomeTimeout            = "timeout"
	OutcomeUnreachable        = "unreachable"
	OutcomeNotPermitted       = "not_permitted"
	OutcomeClosed             = "closed"
	OutcomeAccepted           = "accepted"
	OutcomeHandshakeCompleted = "handshake_completed"
	OutcomeHandshakeFailed    = "handshake_failed"
	OutcomeProxyRefused       = "proxy_refused"
	OutcomeProxyForwarded     = "proxy_forwarded"
	OutcomeProbeError         = "probe_error"
)

const (
	maxAgentLine      = 4096
	maxAgentRequests  = 512
	maxAgentHandles   = 64
	maxTimeoutMS      = 120_000
	maxProxyResponse  = 1024
	checkUserTimeout  = 3 * time.Second
	keepAliveInterval = time.Second
)

var (
	dnsName   = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?){1,8}$`)
	authority = regexp.MustCompile(`^[a-z0-9.-]{1,253}:[1-9][0-9]{0,4}$`)
	handleID  = regexp.MustCompile(`^[cl][0-9]{1,3}$`)
)

// NetRequest is one agent request. Keys are closed: unknown fields are refused.
type NetRequest struct {
	Op         string `json:"op"`
	Address    string `json:"address,omitempty"`
	Port       int    `json:"port,omitempty"`
	TimeoutMS  int    `json:"timeout_ms,omitempty"`
	Target     string `json:"target,omitempty"`
	Connection string `json:"connection,omitempty"`
	Listener   string `json:"listener,omitempty"`
}

// NetResponse is one agent response. RemoteAddress never leaves the collector:
// records carry only the classification derived from it.
type NetResponse struct {
	Schema                  string `json:"schema"`
	Op                      string `json:"op"`
	Outcome                 string `json:"outcome,omitempty"`
	Connection              string `json:"connection,omitempty"`
	Listener                string `json:"listener,omitempty"`
	RemoteAddress           string `json:"remote_address,omitempty"`
	PID                     int    `json:"pid,omitempty"`
	UID                     *int   `json:"uid,omitempty"`
	GID                     *int   `json:"gid,omitempty"`
	ProbeRunnerBinarySHA256 string `json:"probe_runner_binary_sha256,omitempty"`
	Error                   string `json:"error,omitempty"`
}

// NetAgent holds the connections and listeners of one collector session.
type NetAgent struct {
	Dial         func(ctx context.Context, network, address string) (net.Conn, error)
	Listen       func(network, address string) (net.Listener, error)
	RunnerSHA256 func() (string, error)
	ResolvConf   string

	mu          sync.Mutex
	next        int
	connections map[string]net.Conn
	listeners   map[string]*agentListener
}

type agentListener struct {
	listener net.Listener
	accepted chan net.Conn
}

func (a *NetAgent) init() {
	if a.Dial == nil {
		var dialer net.Dialer
		a.Dial = dialer.DialContext
	}
	if a.Listen == nil {
		a.Listen = net.Listen
	}
	if a.RunnerSHA256 == nil {
		a.RunnerSHA256 = RunningExecutableSHA256
	}
	if a.ResolvConf == "" {
		a.ResolvConf = "/etc/resolv.conf"
	}
	if a.connections == nil {
		a.connections = map[string]net.Conn{}
		a.listeners = map[string]*agentListener{}
	}
}

// Close releases every connection and listener the session opened.
func (a *NetAgent) Close() {
	a.mu.Lock()
	defer a.mu.Unlock()
	for id, conn := range a.connections {
		_ = conn.Close()
		delete(a.connections, id)
	}
	for id, listener := range a.listeners {
		_ = listener.listener.Close()
		delete(a.listeners, id)
	}
}

// DecodeNetRequest parses one request line strictly.
func DecodeNetRequest(line []byte) (NetRequest, error) {
	var request NetRequest
	decoder := json.NewDecoder(strings.NewReader(string(line)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		return NetRequest{}, errors.New("probe: malformed agent request")
	}
	if decoder.More() {
		return NetRequest{}, errors.New("probe: trailing agent request data")
	}
	return request, validateNetRequest(request)
}

func validIPv4(address string) bool {
	ip := net.ParseIP(address)
	return ip != nil && ip.To4() != nil && ip.String() == address
}

func validIP(address string) bool {
	ip := net.ParseIP(address)
	return ip != nil && ip.String() == address
}

func validateNetRequest(r NetRequest) error {
	bad := errors.New("probe: agent request fields are not allowed for its op")
	timeout := r.TimeoutMS >= 1 && r.TimeoutMS <= maxTimeoutMS
	endpoint := validIP(r.Address) && r.Port >= 1 && r.Port <= 65535
	none := r.Address == "" && r.Port == 0 && r.Target == "" && r.Connection == "" && r.Listener == ""
	switch r.Op {
	case "hello", "exit":
		if !none || r.TimeoutMS != 0 {
			return bad
		}
	case "connect", "handshake", "establish":
		if !endpoint || !timeout || r.Target != "" || r.Connection != "" || r.Listener != "" {
			return bad
		}
	case "proxy_forward":
		if !endpoint || !timeout || !authority.MatchString(r.Target) || r.Connection != "" || r.Listener != "" {
			return bad
		}
	case "dns":
		if r.Address != "" || r.Port != 0 || !timeout || !dnsName.MatchString(r.Target) || r.Connection != "" || r.Listener != "" {
			return bad
		}
	case "listen":
		if !validIPv4(r.Address) || r.Port < 1 || r.Port > 65535 || r.TimeoutMS != 0 || r.Target != "" || r.Connection != "" || r.Listener != "" {
			return bad
		}
	case "accept":
		if !handleID.MatchString(r.Listener) || r.Listener[0] != 'l' || !timeout || r.Address != "" || r.Port != 0 || r.Target != "" || r.Connection != "" {
			return bad
		}
	case "check":
		if !handleID.MatchString(r.Connection) || r.Connection[0] != 'c' || !timeout || r.Address != "" || r.Port != 0 || r.Target != "" || r.Listener != "" {
			return bad
		}
	default:
		return errors.New("probe: unknown agent op")
	}
	return nil
}

// ClassifyDialError maps a connection attempt's error to a catalog outcome.
// Anything unrecognised is probe_error, which no expectation accepts, so a
// broken probe can never count as a denial.
func ClassifyDialError(err error) string {
	switch {
	case err == nil:
		return OutcomeConnected
	case errors.Is(err, syscall.ECONNREFUSED):
		return OutcomeRefused
	case errors.Is(err, syscall.ECONNRESET), errors.Is(err, syscall.EPIPE), errors.Is(err, syscall.ECONNABORTED):
		return OutcomeReset
	case errors.Is(err, syscall.EPERM), errors.Is(err, syscall.EACCES):
		return OutcomeNotPermitted
	case errors.Is(err, syscall.ENETUNREACH), errors.Is(err, syscall.EHOSTUNREACH),
		errors.Is(err, syscall.EADDRNOTAVAIL), errors.Is(err, syscall.EAFNOSUPPORT),
		errors.Is(err, syscall.ENETDOWN), errors.Is(err, syscall.EHOSTDOWN):
		return OutcomeUnreachable
	case errors.Is(err, syscall.ETIMEDOUT), errors.Is(err, context.DeadlineExceeded), errors.Is(err, os.ErrDeadlineExceeded):
		return OutcomeTimeout
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return OutcomeTimeout
	}
	return OutcomeProbeError
}

// classifyEstablished maps the result of waiting on an established, idle
// connection. A read deadline with no error means the flow is still alive.
func classifyEstablished(n int, err error) string {
	switch {
	case n > 0:
		return OutcomeConnected
	case err == nil:
		return OutcomeProbeError
	case errors.Is(err, os.ErrDeadlineExceeded):
		return OutcomeConnected
	case errors.Is(err, io.EOF):
		return OutcomeClosed
	}
	return ClassifyDialError(err)
}

func (a *NetAgent) store(conn net.Conn) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.connections)+len(a.listeners) >= maxAgentHandles || a.next >= 999 {
		return "", errors.New("probe: agent handle bound reached")
	}
	a.next++
	id := "c" + strconv.Itoa(a.next)
	a.connections[id] = conn
	return id, nil
}

// Handle performs one validated request.
func (a *NetAgent) Handle(ctx context.Context, r NetRequest) NetResponse {
	a.init()
	response := NetResponse{Schema: NetAgentSchema, Op: r.Op}
	if err := validateNetRequest(r); err != nil {
		response.Error = err.Error()
		return response
	}
	timeout := time.Duration(r.TimeoutMS) * time.Millisecond
	address := net.JoinHostPort(r.Address, strconv.Itoa(r.Port))
	switch r.Op {
	case "hello":
		digest, err := a.RunnerSHA256()
		if err != nil || !sha256Hex.MatchString(digest) {
			response.Error = "probe: the running probe binary could not be measured"
			return response
		}
		uid, gid := os.Getuid(), os.Getgid()
		response.PID, response.UID, response.GID = os.Getpid(), &uid, &gid
		response.ProbeRunnerBinarySHA256 = digest
	case "connect", "handshake":
		conn, err := a.dial(ctx, "tcp", address, timeout)
		response.Outcome = ClassifyDialError(err)
		if err == nil {
			// Handshake only: close without writing a byte.
			_ = conn.Close()
		}
		if r.Op == "handshake" {
			if response.Outcome == OutcomeConnected {
				response.Outcome = OutcomeHandshakeCompleted
			} else {
				response.Outcome = OutcomeHandshakeFailed
			}
		}
	case "establish":
		conn, err := a.dial(ctx, "tcp", address, timeout)
		response.Outcome = ClassifyDialError(err)
		if err == nil {
			id, storeErr := a.store(conn)
			if storeErr != nil {
				_ = conn.Close()
				response.Outcome, response.Error = OutcomeProbeError, storeErr.Error()
				return response
			}
			response.Connection = id
		}
	case "proxy_forward":
		response.Outcome = a.proxyForward(ctx, address, r.Target, timeout)
	case "dns":
		response.Outcome = a.dnsQuery(ctx, r.Target, timeout)
	case "listen":
		return a.listen(response, address)
	case "accept":
		return a.accept(ctx, response, r.Listener, timeout)
	case "check":
		response.Outcome = a.check(r.Connection, timeout)
		response.Connection = r.Connection
	case "exit":
		a.Close()
	}
	return response
}

func (a *NetAgent) dial(ctx context.Context, network, address string, timeout time.Duration) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return a.Dial(ctx, network, address)
}

func (a *NetAgent) proxyForward(ctx context.Context, proxy, target string, timeout time.Duration) string {
	conn, err := a.dial(ctx, "tcp", proxy, timeout)
	if err != nil {
		return ClassifyDialError(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	request := "CONNECT " + target + " HTTP/1.1\r\nHost: " + target + "\r\n\r\n"
	if _, err := io.WriteString(conn, request); err != nil {
		return ClassifyDialError(err)
	}
	line, err := bufio.NewReaderSize(io.LimitReader(conn, maxProxyResponse), maxProxyResponse).ReadString('\n')
	if err != nil {
		if errors.Is(err, io.EOF) {
			return OutcomeClosed
		}
		return ClassifyDialError(err)
	}
	fields := strings.Fields(line)
	if len(fields) < 2 || !strings.HasPrefix(fields[0], "HTTP/1.") || len(fields[1]) != 3 {
		return OutcomeProbeError
	}
	switch code := fields[1]; {
	case code[0] == '2':
		return OutcomeProxyForwarded
	case code == "403" || code == "405" || code == "407":
		return OutcomeProxyRefused
	}
	return OutcomeProbeError
}

// resolver returns the first IPv4 nameserver the candidate's resolv.conf names.
func (a *NetAgent) resolver() (string, error) {
	raw, err := os.ReadFile(a.ResolvConf)
	if err != nil || len(raw) > 64<<10 {
		return "", errors.New("probe: resolver configuration unavailable")
	}
	for _, line := range strings.Split(string(raw), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[0] == "nameserver" && validIPv4(fields[1]) {
			return net.JoinHostPort(fields[1], "53"), nil
		}
	}
	return "", errors.New("probe: no IPv4 nameserver configured")
}

// EncodeDNSQuestion builds one recursive A question with the given id.
func EncodeDNSQuestion(id uint16, name string) []byte {
	message := make([]byte, 12, 12+len(name)+6)
	binary.BigEndian.PutUint16(message[0:], id)
	binary.BigEndian.PutUint16(message[2:], 0x0100) // RD
	binary.BigEndian.PutUint16(message[4:], 1)
	for _, label := range strings.Split(name, ".") {
		message = append(message, byte(len(label)))
		message = append(message, label...)
	}
	return append(message, 0, 0, 1, 0, 1)
}

// ClassifyDNSResponse maps a resolver answer: any answer record means the name
// resolved (connected); an error code or an empty answer means the resolver
// refused to resolve it.
func ClassifyDNSResponse(id uint16, response []byte) string {
	if len(response) < 12 || binary.BigEndian.Uint16(response[0:]) != id {
		return OutcomeProbeError
	}
	flags := binary.BigEndian.Uint16(response[2:])
	if flags&0x8000 == 0 {
		return OutcomeProbeError
	}
	if flags&0x000f == 0 && binary.BigEndian.Uint16(response[6:]) > 0 {
		return OutcomeConnected
	}
	return OutcomeRefused
}

func (a *NetAgent) dnsQuery(ctx context.Context, name string, timeout time.Duration) string {
	server, err := a.resolver()
	if err != nil {
		return OutcomeProbeError
	}
	conn, err := a.dial(ctx, "udp", server, timeout)
	if err != nil {
		return ClassifyDialError(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	const id = 0x4e42
	if _, err := conn.Write(EncodeDNSQuestion(id, name)); err != nil {
		return ClassifyDialError(err)
	}
	buffer := make([]byte, 1500)
	n, err := conn.Read(buffer)
	if err != nil {
		return ClassifyDialError(err)
	}
	return ClassifyDNSResponse(id, buffer[:n])
}

func (a *NetAgent) listen(response NetResponse, address string) NetResponse {
	listener, err := a.Listen("tcp4", address)
	if err != nil {
		response.Outcome, response.Error = OutcomeProbeError, "probe: listener unavailable"
		return response
	}
	a.mu.Lock()
	if len(a.connections)+len(a.listeners) >= maxAgentHandles || a.next >= 999 {
		a.mu.Unlock()
		_ = listener.Close()
		response.Outcome, response.Error = OutcomeProbeError, "probe: agent handle bound reached"
		return response
	}
	a.next++
	id := "l" + strconv.Itoa(a.next)
	entry := &agentListener{listener: listener, accepted: make(chan net.Conn, 16)}
	a.listeners[id] = entry
	a.mu.Unlock()
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				close(entry.accepted)
				return
			}
			select {
			case entry.accepted <- conn:
			default:
				_ = conn.Close()
			}
		}
	}()
	response.Outcome, response.Listener = OutcomeAccepted, id
	return response
}

func (a *NetAgent) accept(ctx context.Context, response NetResponse, id string, timeout time.Duration) NetResponse {
	a.mu.Lock()
	entry := a.listeners[id]
	a.mu.Unlock()
	response.Listener = id
	if entry == nil {
		response.Outcome, response.Error = OutcomeProbeError, "probe: unknown listener"
		return response
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case conn, ok := <-entry.accepted:
		if !ok {
			response.Outcome = OutcomeProbeError
			return response
		}
		host, _, err := net.SplitHostPort(conn.RemoteAddr().String())
		connection, storeErr := a.store(conn)
		if err != nil || storeErr != nil {
			_ = conn.Close()
			response.Outcome = OutcomeProbeError
			return response
		}
		response.Outcome, response.Connection, response.RemoteAddress = OutcomeAccepted, connection, host
	case <-timer.C:
		response.Outcome = OutcomeTimeout
	case <-ctx.Done():
		response.Outcome = OutcomeProbeError
	}
	return response
}

// check waits on an established idle connection without sending payload. TCP
// keepalive probes carry no data; TCP_USER_TIMEOUT bounds how long unacknowledged
// probes may go unanswered, so a flow whose packets the kernel now rejects
// fails within the wait instead of looking alive.
func (a *NetAgent) check(id string, timeout time.Duration) string {
	a.mu.Lock()
	conn := a.connections[id]
	a.mu.Unlock()
	if conn == nil {
		return OutcomeProbeError
	}
	if tcp, ok := conn.(*net.TCPConn); ok {
		if err := tcp.SetKeepAliveConfig(net.KeepAliveConfig{
			Enable: true, Idle: keepAliveInterval, Interval: keepAliveInterval, Count: 2,
		}); err != nil {
			return OutcomeProbeError
		}
		if err := setUserTimeout(tcp, checkUserTimeout); err != nil {
			return OutcomeProbeError
		}
	}
	_ = conn.SetReadDeadline(time.Now().Add(timeout))
	buffer := make([]byte, 1)
	n, err := conn.Read(buffer)
	return classifyEstablished(n, err)
}

// ServeNetAgent runs the line protocol until exit, end of input or the request
// bound. Every response is one JSON line.
func ServeNetAgent(ctx context.Context, agent *NetAgent, input io.Reader, output io.Writer) error {
	defer agent.Close()
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, maxAgentLine), maxAgentLine)
	encoder := json.NewEncoder(output)
	for count := 0; scanner.Scan(); count++ {
		if count >= maxAgentRequests {
			return errors.New("probe: agent request bound reached")
		}
		request, err := DecodeNetRequest(scanner.Bytes())
		var response NetResponse
		if err != nil {
			response = NetResponse{Schema: NetAgentSchema, Op: "invalid", Error: err.Error()}
		} else {
			response = agent.Handle(ctx, request)
		}
		if err := encoder.Encode(response); err != nil {
			return err
		}
		if request.Op == "exit" && err == nil {
			return nil
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("probe: agent input: %w", err)
	}
	return nil
}

// NetPlan is a fixed list of one-shot attempts for a position the collector
// cannot hold a session with (ExecStopPost in the worker cgroup).
type NetPlan struct {
	Schema   string       `json:"schema"`
	Requests []NetRequest `json:"requests"`
}

// NetReport is a plan's result, read back by the collector.
type NetReport struct {
	Schema                  string        `json:"schema"`
	PID                     int           `json:"pid"`
	UID                     int           `json:"uid"`
	GID                     int           `json:"gid"`
	ProbeRunnerBinarySHA256 string        `json:"probe_runner_binary_sha256"`
	Responses               []NetResponse `json:"responses"`
}

// DecodeNetPlan parses a plan strictly; only connect and handshake attempts
// are allowed, at most 64 of them.
func DecodeNetPlan(raw []byte) (NetPlan, error) {
	var plan NetPlan
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if len(raw) > 64<<10 || decoder.Decode(&plan) != nil || decoder.More() {
		return NetPlan{}, errors.New("probe: malformed network plan")
	}
	if plan.Schema != NetPlanSchema || len(plan.Requests) == 0 || len(plan.Requests) > 64 {
		return NetPlan{}, errors.New("probe: network plan schema or size is not allowed")
	}
	for _, request := range plan.Requests {
		if request.Op != "connect" && request.Op != "handshake" {
			return NetPlan{}, errors.New("probe: network plan op is not allowed")
		}
		if err := validateNetRequest(request); err != nil {
			return NetPlan{}, err
		}
	}
	return plan, nil
}

// RunNetPlan performs every attempt in order and reports them.
func RunNetPlan(ctx context.Context, agent *NetAgent, plan NetPlan) (NetReport, error) {
	agent.init()
	defer agent.Close()
	digest, err := agent.RunnerSHA256()
	if err != nil || !sha256Hex.MatchString(digest) {
		return NetReport{}, errors.New("probe: the running probe binary could not be measured")
	}
	report := NetReport{
		Schema: NetReportSchema, PID: os.Getpid(), UID: os.Getuid(), GID: os.Getgid(),
		ProbeRunnerBinarySHA256: digest,
	}
	for _, request := range plan.Requests {
		report.Responses = append(report.Responses, agent.Handle(ctx, request))
	}
	return report, nil
}
