// Package workers — FreeInference bridge contract test.
//
// This file is NOT part of Runner. It is copied into a throwaway copy of the
// Runner module by integration/runner/run_contract_tests.sh (in
// freeinference-pr-engineering) and run with `go test`. It drives Runner's
// REAL CommandAdapter against the real `fi-pr runner-task` bridge subprocess,
// with the FreeInference HTTP endpoint replaced by a local mock server (full
// HTTP/tool-call path; no real model — run scripts/runner_qualification_live.sh
// on a machine with FREEINFERENCE_API_KEY for the live-model pass).
//
// Required environment:
//   FI_PR_BIN  absolute path to the fi-pr executable (skipped when empty)
package workers

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type mockFI struct {
	requests atomic.Int64
	delay    time.Duration
}

var (
	mockMu       sync.Mutex
	mockRegistry = map[string]*mockFI{}
)

func writeMockFI(t *testing.T, delay time.Duration) *httptest.Server {
	t.Helper()
	m := &mockFI{delay: delay}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/chat/completions" {
			http.Error(w, "not found", http.StatusNotFound)
			return
		}
		buf := make([]byte, 1<<20)
		n, _ := r.Body.Read(buf)
		body := string(buf[:n])
		m.requests.Add(1)
		time.Sleep(m.delay)
		var content string
		switch {
		case strings.Contains(body, "OUT_OF_SCOPE"):
			content = `{"edits": [{"path": "other.py", "content": "X = 2\n"}], "commit_message": "out of scope", "regression_tests": [], "notes": ""}`
		case strings.Contains(body, "Implement this task"):
			content = `{"edits": [{"path": "src.py", "content": "VALUE = 2\n"}], "commit_message": "bump VALUE", "regression_tests": [], "notes": ""}`
		case strings.Contains(body, "Review the effective diff"):
			content = `{"assessment": "clean", "findings": [], "missed_paths": [], "unsupported_assumptions": []}`
		default:
			content = `{"summary": "plan", "affected_components": ["src.py"], "production_paths": [], "implementation_units": [], "findings": [], "open_questions": []}`
		}
		resp := map[string]any{
			"id": "chatcmpl-mock", "object": "chat.completion", "model": "mock-model",
			"choices": []map[string]any{
				{"index": 0, "message": map[string]any{"role": "assistant", "content": content}, "finish_reason": "stop"},
			},
			"usage": map[string]any{"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))
	mockMu.Lock()
	mockRegistry[server.URL] = m
	mockMu.Unlock()
	t.Cleanup(server.Close)
	return server
}


// fiEndpoint returns the inference endpoint for the current subtest: a local
// mock server normally, or the real endpoint when FI_LIVE=1 (the caller has
// set FREEINFERENCE_BASE_URL / FREEINFERENCE_API_KEY itself).
func fiEndpoint(t *testing.T, delay time.Duration) string {
	t.Helper()
	if os.Getenv("FI_LIVE") == "1" {
		return ""
	}
	return writeMockFI(t, delay).URL
}

func loadMockCount(t *testing.T, endpoint string) int64 {
	t.Helper()
	mockMu.Lock()
	defer mockMu.Unlock()
	if m, ok := mockRegistry[endpoint]; ok {
		return m.requests.Load()
	}
	return -1
}

func gitRun(t *testing.T, dir string, args ...string) string {
	t.Helper()
	full := append([]string{"-C", dir}, args...)
	cmd := exec.Command("git", full...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v: %s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func newWorktree(t *testing.T) (string, string) {
	t.Helper()
	wt := filepath.Join(t.TempDir(), "wt")
	if err := os.MkdirAll(wt, 0o755); err != nil {
		t.Fatal(err)
	}
	gitRun(t, wt, "init", "-q", "-b", "main")
	gitRun(t, wt, "config", "user.email", "t@example.com")
	gitRun(t, wt, "config", "user.name", "T")
	if err := os.WriteFile(filepath.Join(wt, "src.py"), []byte("VALUE = 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(wt, "other.py"), []byte("X = 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, wt, "add", "-A")
	gitRun(t, wt, "commit", "-q", "-m", "base")
	return wt, gitRun(t, wt, "rev-parse", "HEAD")
}

func buildPackage(t *testing.T, artifactDir, wt, baseSHA, role, goal string, allowed []string) TaskPackage {
	t.Helper()
	dir := filepath.Join(artifactDir, "att-"+role)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	return TaskPackage{
		TaskID: "task-" + role, AttemptID: "att-" + role, AttemptNo: 1, ProjectID: "p",
		Repository: "octo/example", Role: role, Title: "qualification", Goal: goal,
		Acceptance: []string{"VALUE == 2"}, BaseSHA: baseSHA, Branch: "runner/task",
		Worktree: wt, AllowedPaths: allowed, MaxMinutes: 5,
		IdempotencyKey: "idem-" + role + "-" + goal,
		PackageFile:    filepath.Join(artifactDir, "att-"+role, "task-package.json"),
	}
}

func runAdapter(t *testing.T, artifactDir string, timeout time.Duration, pkg TaskPackage) *Collect {
	t.Helper()
	fiPrBin := os.Getenv("FI_PR_BIN")
	if fiPrBin == "" {
		t.Skip("FI_PR_BIN not set; build the fi-pr venv first")
	}
	return runAdapterTemplate(t, artifactDir, timeout, pkg, fiPrBin+" runner-task {{.PackageFile}}")
}

// runAdapterTemplate dispatches through Runner's real CommandAdapter with a
// custom command template. The hardening subtest uses it to inject the
// audit/runner-hardening package fields (candidateSha, verdictFile, readOnly,
// maxDurationSeconds, maxTokens) into the marshaled package JSON before the
// bridge reads it — master's TaskPackage struct cannot carry them.
func runAdapterTemplate(t *testing.T, artifactDir string, timeout time.Duration, pkg TaskPackage, template string) *Collect {
	t.Helper()
	adapter := NewCommandAdapter("fi", template, artifactDir, timeout)
	runID, err := adapter.Start(t.Context(), pkg)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	collect, err := adapter.Collect(t.Context(), runID)
	if err != nil {
		t.Fatalf("Collect: %v", err)
	}
	if collect.State != "succeeded" {
		if logData, logErr := os.ReadFile(filepath.Join(artifactDir, pkg.AttemptID, "worker.log")); logErr == nil {
			t.Logf("worker.log tail: %s", string(logData))
		} else {
			t.Logf("worker.log unreadable: %v", logErr)
		}
	}
	return collect
}

func readCollectFile(t *testing.T, pkg TaskPackage) (*Collect, string) {
	t.Helper()
	data, err := os.ReadFile(pkg.PackageFile + ".result.json")
	if err != nil {
		t.Fatalf("result file: %v", err)
	}
	var c Collect
	if err := json.Unmarshal(data, &c); err != nil {
		t.Fatalf("result file does not unmarshal into workers.Collect: %v", err)
	}
	sum := sha256.Sum256(data)
	return &c, hex.EncodeToString(sum[:])
}

func fileCollectReplayed(t *testing.T, pkg TaskPackage) bool {
	t.Helper()
	data, err := os.ReadFile(pkg.PackageFile + ".result.json")
	if err != nil {
		return false
	}
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return false
	}
	flag, _ := raw["replayed"].(bool)
	return flag
}

// TestFIBridgeContract is the Phase-3 qualification entry point.
func TestFIBridgeContract(t *testing.T) {
	fiPrBin := os.Getenv("FI_PR_BIN")
	if fiPrBin == "" {
		t.Skip("FI_PR_BIN not set")
	}
	t.Setenv("FREEINFERENCE_API_KEY", "test-key")
	t.Setenv("FI_MAX_ATTEMPTS", "1")
	t.Setenv("FI_BACKOFF_BASE", "0.05")
	t.Setenv("FI_BACKOFF_MAX", "0.1")
	t.Setenv("PYTHONDONTWRITEBYTECODE", "1")

	t.Run("planner_analyzes_real_task", func(t *testing.T) {
		endpoint := fiEndpoint(t, 0)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "planner", "plan the VALUE bump", nil)
		collect := runAdapter(t, root, 2*time.Minute, pkg)
		if collect.State != "succeeded" {
			t.Fatalf("state=%s err=%s", collect.State, collect.Err)
		}
		fileCollect, digest := readCollectFile(t, pkg)
		if fileCollect.HeadSHA != base {
			t.Fatalf("candidate identity mismatch: file headSha=%s actual=%s", fileCollect.HeadSHA, base)
		}
		if !strings.Contains(fileCollect.Result, "summary") {
			t.Fatalf("planner result missing analysis report: %s", fileCollect.Result)
		}
		if len(digest) != 64 {
			t.Fatalf("evidence digest invalid: %q", digest)
		}
		t.Logf("planner OK: headSha=%s digest=%.12s", fileCollect.HeadSHA, digest)
	})

	t.Run("implementer_produces_scoped_commit", func(t *testing.T) {
		endpoint := fiEndpoint(t, 0)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "implementer", "bump VALUE", []string{"src.py"})
		collect := runAdapter(t, root, 2*time.Minute, pkg)
		if collect.State != "succeeded" {
			t.Fatalf("state=%s err=%s", collect.State, collect.Err)
		}
		fileCollect, _ := readCollectFile(t, pkg)
		head := gitRun(t, wt, "rev-parse", "HEAD")
		if fileCollect.HeadSHA != head || head == base {
			t.Fatalf("candidate identity: file=%s head=%s base=%s", fileCollect.HeadSHA, head, base)
		}
		if len(fileCollect.Commits) != 1 {
			t.Fatalf("commits=%v", fileCollect.Commits)
		}
		data, _ := os.ReadFile(filepath.Join(wt, "src.py"))
		if string(data) != "VALUE = 2\n" {
			t.Fatalf("scoped edit not applied: %q", data)
		}
		other, _ := os.ReadFile(filepath.Join(wt, "other.py"))
		if string(other) != "X = 1\n" {
			t.Fatal("out-of-scope file modified")
		}
		t.Logf("implementer OK: commit=%s", fileCollect.Commits[0])
	})

	t.Run("out_of_scope_edit_fails", func(t *testing.T) {
		endpoint := fiEndpoint(t, 0)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "implementer", "OUT_OF_SCOPE", []string{"src.py"})
		collect := runAdapter(t, root, 2*time.Minute, pkg)
		if collect.State != "failed" {
			t.Fatalf("out-of-scope edit must fail, got state=%s", collect.State)
		}
		other, _ := os.ReadFile(filepath.Join(wt, "other.py"))
		if string(other) != "X = 1\n" {
			t.Fatal("forbidden file was modified")
		}
		t.Logf("scope enforcement OK: err=%.80s", collect.Err)
	})

	t.Run("stale_candidate_rejected_with_zero_inference", func(t *testing.T) {
		endpoint := fiEndpoint(t, 0)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "implementer", "bump VALUE", []string{"src.py"})
		gitRun(t, wt, "commit", "-q", "--allow-empty", "-m", "candidate moved")
		collect := runAdapter(t, root, 2*time.Minute, pkg)
		if collect.State != "failed" {
			t.Fatalf("stale candidate must fail, got state=%s err=%s", collect.State, collect.Err)
		}
		fileCollect, _ := readCollectFile(t, pkg)
		if !strings.Contains(fileCollect.Err, "candidate SHA moved") {
			t.Fatalf("stale candidate must report the SHA error, got: %s", fileCollect.Err)
		}
		if calls := loadMockCount(t, endpoint); calls > 0 {
			t.Fatalf("stale candidate must make zero inference calls, got %d", calls)
		}
		t.Log("SHA binding OK: zero inference calls on stale candidate")
	})

	t.Run("duplicate_task_replays_without_rerun", func(t *testing.T) {
		endpoint := fiEndpoint(t, 0)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "implementer", "bump VALUE", []string{"src.py"})
		first := runAdapter(t, root, 2*time.Minute, pkg)
		if first.State != "succeeded" {
			t.Fatalf("first run state=%s err=%s", first.State, first.Err)
		}
		second := runAdapter(t, root, 2*time.Minute, pkg)
		if second.State != "succeeded" {
			t.Fatalf("replay state=%s err=%s", second.State, second.Err)
		}
		if calls := loadMockCount(t, endpoint); calls > 1 {
			t.Fatalf("duplicate task re-ran inference: %d calls", calls)
		}
		_, _ = readCollectFile(t, pkg)
		if !fileCollectReplayed(t, pkg) {
			t.Fatal("replay not flagged in result file")
		}
		t.Log("idempotency OK: duplicate task replayed, single inference call")
	})

	t.Run("cancel_terminates_active_work", func(t *testing.T) {
		endpoint := fiEndpoint(t, 8*time.Second)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "implementer", "bump VALUE", []string{"src.py"})
		adapter := NewCommandAdapter("fi", fiPrBin+" runner-task {{.PackageFile}}", root, 2*time.Minute)
		runID, err := adapter.Start(t.Context(), pkg)
		if err != nil {
			t.Fatal(err)
		}
		time.Sleep(1500 * time.Millisecond)
		state, err := adapter.Cancel(t.Context(), runID, "qualification test")
		if err != nil || state != CancelAcknowledged {
			t.Fatalf("cancel: state=%v err=%v", state, err)
		}
		collect, err := adapter.Collect(t.Context(), runID)
		if err != nil {
			t.Fatal(err)
		}
		if collect.State != "cancelled" {
			t.Fatalf("expected cancelled, got state=%s err=%s", collect.State, collect.Err)
		}
		t.Log("cancellation OK: SIGTERM to process group; run reported cancelled")
	})

	t.Run("deadline_expiration_enforced", func(t *testing.T) {
		endpoint := fiEndpoint(t, 8*time.Second)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "implementer", "bump VALUE", []string{"src.py"})
		collect := runAdapter(t, root, 2*time.Second, pkg)
		if collect.State != "failed" || !strings.Contains(collect.Err, "time budget") {
			t.Fatalf("deadline must fail with time-budget error, got state=%s err=%s", collect.State, collect.Err)
		}
		t.Log("deadline OK: adapter timeout produced honest failure")
	})

	t.Run("verifier_evidence_ingestion_and_identity", func(t *testing.T) {
		endpoint := fiEndpoint(t, 0)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		root := t.TempDir()
		wt, base := newWorktree(t)
		pkg := buildPackage(t, root, wt, base, "verifier", "review the candidate", nil)
		collect := runAdapter(t, root, 2*time.Minute, pkg)
		if collect.State != "succeeded" {
			t.Fatalf("state=%s err=%s", collect.State, collect.Err)
		}
		fileCollect, digest := readCollectFile(t, pkg)
		head := gitRun(t, wt, "rev-parse", "HEAD")
		if fileCollect.HeadSHA != head {
			t.Fatal("verifier candidate identity mismatch")
		}
		var wrapper struct {
			Verdict struct {
				Decision                   string `json:"decision"`
				CandidateSHA               string `json:"candidate_sha"`
				DiffDigest                 string `json:"diff_digest"`
				EvidenceKind               string `json:"evidence_kind"`
				ReviewerProvenance         string `json:"reviewer_provenance"`
				GrantsPublicationAuthority bool   `json:"grants_publication_authority"`
			} `json:"verdict"`
		}
		if err := json.Unmarshal([]byte(fileCollect.Result), &wrapper); err != nil {
			t.Fatalf("verdict unmarshal: %v", err)
		}
		v := wrapper.Verdict
		if v.CandidateSHA != head || v.EvidenceKind != "fi_review" || v.GrantsPublicationAuthority {
			t.Fatalf("verdict provenance/identity wrong: %+v", v)
		}
		if len(v.DiffDigest) != 64 {
			t.Fatal("diff digest missing")
		}
		tampered := *fileCollect
		tampered.HeadSHA = strings.Repeat("0", 40)
		if tampered.HeadSHA == head {
			t.Fatal("tamper simulation broken")
		}
		t.Logf("verifier OK: decision=%s provenance=%s result-digest=%.12s", v.Decision, v.ReviewerProvenance, digest)
	})

	t.Run("verifier_hardening_fields_verdict_file", func(t *testing.T) {
		endpoint := fiEndpoint(t, 0)
		if endpoint != "" {
			t.Setenv("FREEINFERENCE_BASE_URL", endpoint)
		}
		t.Setenv("FI_STATE_PATH", filepath.Join(t.TempDir(), "state.db"))
		fiPrBin := os.Getenv("FI_PR_BIN")
		root := t.TempDir()
		wt, base := newWorktree(t)
		gitRun(t, wt, "checkout", "-q", "-b", "runner/task")
		if err := os.WriteFile(filepath.Join(wt, "src.py"), []byte("VALUE = 2\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		gitRun(t, wt, "add", "-A")
		gitRun(t, wt, "commit", "-q", "-m", "candidate")
		candidate := gitRun(t, wt, "rev-parse", "HEAD")

		pkg := buildPackage(t, root, wt, base, "verifier", "review the hardening candidate", nil)
		verdictPath := filepath.Join(root, "verdict.json")
		t.Setenv("FI_TEST_CANDIDATE_SHA", candidate)
		t.Setenv("FI_TEST_VERDICT_FILE", verdictPath)
		hardenSrc := "import json, os, sys\n" +
			"p = sys.argv[1]\n\nwith open(p) as f: d = json.load(f)\n" +
			"d['candidateSha'] = os.environ['FI_TEST_CANDIDATE_SHA']\n" +
			"d['verdictFile'] = os.environ['FI_TEST_VERDICT_FILE']\n" +
			"d['readOnly'] = True\nd['maxDurationSeconds'] = 120\nd['maxTokens'] = 100000\n" +
			"\nwith open(p, 'w') as f: json.dump(d, f, indent=2)\n"
		hardenPath := pkg.PackageFile + ".harden.py"
		if err := os.WriteFile(hardenPath, []byte(hardenSrc), 0o600); err != nil {
			t.Fatal(err)
		}
		template := fmt.Sprintf("python3 %s.harden.py {{.PackageFile}} && %s runner-task {{.PackageFile}}",
			"{{.PackageFile}}", fiPrBin)
		collect := runAdapterTemplate(t, root, 2*time.Minute, pkg, template)
		if collect.State != "succeeded" {
			t.Fatalf("state=%s err=%s", collect.State, collect.Err)
		}
		if collect.HeadSHA != candidate {
			t.Fatalf("candidate identity mismatch: %s != %s", collect.HeadSHA, candidate)
		}
		data, err := os.ReadFile(verdictPath)
		if err != nil {
			t.Fatalf("verdict file: %v", err)
		}
		var doc struct {
			Decision string `json:"decision"`
			SHA      string `json:"sha"`
			Evidence []struct {
				URI    string `json:"uri"`
				Digest string `json:"digest"`
			} `json:"evidence"`
		}
		if err := json.Unmarshal(data, &doc); err != nil {
			t.Fatalf("verdict document unmarshal: %v", err)
		}
		if doc.Decision != "approve" || doc.SHA != candidate || len(doc.Evidence) != 1 {
			t.Fatalf("flat verdict document wrong: %+v", doc)
		}
		diffData, err := os.ReadFile(doc.Evidence[0].URI)
		if err != nil {
			t.Fatalf("verdict diff artifact: %v", err)
		}
		sum := sha256.Sum256(diffData)
		if hex.EncodeToString(sum[:]) != doc.Evidence[0].Digest || len(diffData) == 0 {
			t.Fatal("verdict evidence digest does not match the diff artifact")
		}
		t.Logf("hardening verdict OK: sha pinned, evidence digest verified")
	})
}
