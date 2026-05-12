// Oracle is a tiny JSON-over-stdio bridge that exposes whatsmeow's internal
// primitives (binary codec, LtHash, media keys, etc.) so the Python port
// can diff its outputs against a known-good reference byte-for-byte.
//
// Protocol: one JSON request per line on stdin, one JSON response per line
// on stdout. Errors go to stderr and as {"error": "..."} on stdout.
//
//	{"op":"encode_node","arg":{<node JSON>}}      → {"ok":{"bytes":"<base64>"}}
//	{"op":"decode_node","arg":{"bytes":"<b64>"}}  → {"ok":{"node":{...}}}
//	{"op":"lthash_apply","arg":{"base":"b64","add":["b64"...],"sub":["b64"...]}}
//	{"op":"derive_media_keys","arg":{"media_key":"b64","app_info":"<string>"}}
//	{"op":"ping"}                                 → {"ok":{"pong":true}}
package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"

	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/appstate/lthash"
	"go.mau.fi/whatsmeow/socket"
	"go.mau.fi/whatsmeow/util/hkdfutil"

	"go.mau.fi/libsignal/ecc"
	"golang.org/x/crypto/curve25519"
)

type request struct {
	Op  string          `json:"op"`
	Arg json.RawMessage `json:"arg,omitempty"`
}

type response struct {
	OK    map[string]any `json:"ok,omitempty"`
	Error string         `json:"error,omitempty"`
}

func main() {
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1<<20), 1<<24)
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()

	for in.Scan() {
		line := in.Bytes()
		if len(line) == 0 {
			continue
		}
		var req request
		if err := json.Unmarshal(line, &req); err != nil {
			writeErr(out, fmt.Errorf("bad request: %w", err))
			continue
		}
		switch req.Op {
		case "ping":
			writeOK(out, map[string]any{"pong": true})
		case "encode_node":
			handleEncodeNode(out, req.Arg)
		case "decode_node":
			handleDecodeNode(out, req.Arg)
		case "lthash_apply":
			handleLtHashApply(out, req.Arg)
		case "derive_media_keys":
			handleDeriveMediaKeys(out, req.Arg)
		case "noise_trace":
			handleNoiseTrace(out, req.Arg)
		case "xeddsa_sign":
			handleXeddsaSign(out, req.Arg)
		case "xeddsa_verify":
			handleXeddsaVerify(out, req.Arg)
		default:
			writeErr(out, fmt.Errorf("unknown op %q", req.Op))
		}
	}
	if err := in.Err(); err != nil && err != io.EOF {
		fmt.Fprintln(os.Stderr, "scan error:", err)
		os.Exit(1)
	}
}

func writeOK(out *bufio.Writer, payload map[string]any) {
	b, _ := json.Marshal(response{OK: payload})
	out.Write(b)
	out.WriteByte('\n')
	out.Flush()
}

func writeErr(out *bufio.Writer, err error) {
	b, _ := json.Marshal(response{Error: err.Error()})
	out.Write(b)
	out.WriteByte('\n')
	out.Flush()
}

// encode_node: { node: {...} } → { bytes: "<base64>" }
// The node JSON uses whatsmeow's Node.UnmarshalJSON schema.
func handleEncodeNode(out *bufio.Writer, arg json.RawMessage) {
	var wrap struct {
		Node json.RawMessage `json:"node"`
	}
	if err := json.Unmarshal(arg, &wrap); err != nil {
		writeErr(out, err)
		return
	}
	var n waBinary.Node
	if err := json.Unmarshal(wrap.Node, &n); err != nil {
		writeErr(out, fmt.Errorf("parse node: %w", err))
		return
	}
	b, err := waBinary.Marshal(n)
	if err != nil {
		writeErr(out, err)
		return
	}
	writeOK(out, map[string]any{"bytes": base64.StdEncoding.EncodeToString(b)})
}

// decode_node: { bytes: "<base64>" } → { node: {...} }
// Input bytes come from Marshal, which prefixes a compression flag byte.
// Unpack strips (and optionally zlib-decompresses); only Unmarshal-ready
// bytes go into the codec.
func handleDecodeNode(out *bufio.Writer, arg json.RawMessage) {
	var wrap struct {
		Bytes string `json:"bytes"`
	}
	if err := json.Unmarshal(arg, &wrap); err != nil {
		writeErr(out, err)
		return
	}
	data, err := base64.StdEncoding.DecodeString(wrap.Bytes)
	if err != nil {
		writeErr(out, fmt.Errorf("base64: %w", err))
		return
	}
	unpacked, err := waBinary.Unpack(data)
	if err != nil {
		writeErr(out, fmt.Errorf("unpack: %w", err))
		return
	}
	n, err := waBinary.Unmarshal(unpacked)
	if err != nil {
		writeErr(out, err)
		return
	}
	writeOK(out, map[string]any{"node": nodeToJSON(n)})
}

// nodeToJSON converts a decoded Node into a stable JSON shape that the Python
// decoder can match against. JIDs become strings, byte content becomes base64.
func nodeToJSON(n *waBinary.Node) map[string]any {
	m := map[string]any{"Tag": n.Tag}
	if len(n.Attrs) > 0 {
		attrs := map[string]any{}
		for k, v := range n.Attrs {
			attrs[k] = attrValueToJSON(v)
		}
		m["Attrs"] = attrs
	}
	switch c := n.Content.(type) {
	case nil:
		// omit
	case []byte:
		m["Content"] = base64.StdEncoding.EncodeToString(c)
	case []waBinary.Node:
		arr := make([]map[string]any, len(c))
		for i := range c {
			arr[i] = nodeToJSON(&c[i])
		}
		m["Content"] = arr
	default:
		m["Content"] = fmt.Sprintf("%T:%v", c, c)
	}
	return m
}

func attrValueToJSON(v any) any {
	switch tv := v.(type) {
	case fmt.Stringer:
		return tv.String()
	default:
		return v
	}
}

// lthash_apply: { base:"b64", sub:["b64"...], add:["b64"...] }
// Applies WAPatchIntegrity subtract-then-add over the 128-byte base state.
func handleLtHashApply(out *bufio.Writer, arg json.RawMessage) {
	var wrap struct {
		Base string   `json:"base"`
		Sub  []string `json:"sub"`
		Add  []string `json:"add"`
	}
	if err := json.Unmarshal(arg, &wrap); err != nil {
		writeErr(out, err)
		return
	}
	base, err := base64.StdEncoding.DecodeString(wrap.Base)
	if err != nil {
		writeErr(out, fmt.Errorf("base: %w", err))
		return
	}
	if len(base) != 128 {
		writeErr(out, fmt.Errorf("base must be 128 bytes, got %d", len(base)))
		return
	}
	decode := func(ss []string) ([][]byte, error) {
		r := make([][]byte, len(ss))
		for i, s := range ss {
			b, err := base64.StdEncoding.DecodeString(s)
			if err != nil {
				return nil, err
			}
			r[i] = b
		}
		return r, nil
	}
	subs, err := decode(wrap.Sub)
	if err != nil {
		writeErr(out, err)
		return
	}
	adds, err := decode(wrap.Add)
	if err != nil {
		writeErr(out, err)
		return
	}
	result := lthash.WAPatchIntegrity.SubtractThenAdd(base, subs, adds)
	writeOK(out, map[string]any{"result": base64.StdEncoding.EncodeToString(result)})
}

// xeddsa_sign: { priv:"b64 (32)", message:"b64" } → { pub:"b64", signature:"b64 (64)" }
// Uses libsignal's XEdDSA (Curve25519 key, Ed25519-shape signature). The
// underlying impl reads 64 bytes of randomness internally.
func handleXeddsaSign(out *bufio.Writer, arg json.RawMessage) {
	var req struct {
		Priv    string `json:"priv"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(arg, &req); err != nil {
		writeErr(out, err)
		return
	}
	priv, err := base64.StdEncoding.DecodeString(req.Priv)
	if err != nil || len(priv) != 32 {
		writeErr(out, fmt.Errorf("priv: need 32B: %w", err))
		return
	}
	msg, err := base64.StdEncoding.DecodeString(req.Message)
	if err != nil {
		writeErr(out, fmt.Errorf("message: %w", err))
		return
	}
	var privArr [32]byte
	copy(privArr[:], priv)
	// Derive Curve25519 public key for the caller's convenience.
	pub, err := curve25519.X25519(priv, curve25519.Basepoint)
	if err != nil {
		writeErr(out, err)
		return
	}
	sig := ecc.CalculateSignature(ecc.NewDjbECPrivateKey(privArr), msg)
	writeOK(out, map[string]any{
		"pub":       base64.StdEncoding.EncodeToString(pub),
		"signature": base64.StdEncoding.EncodeToString(sig[:]),
	})
}

// xeddsa_verify: { pub:"b64 (32)", message:"b64", signature:"b64 (64)" } → { valid: bool }
func handleXeddsaVerify(out *bufio.Writer, arg json.RawMessage) {
	var req struct {
		Pub       string `json:"pub"`
		Message   string `json:"message"`
		Signature string `json:"signature"`
	}
	if err := json.Unmarshal(arg, &req); err != nil {
		writeErr(out, err)
		return
	}
	pub, err := base64.StdEncoding.DecodeString(req.Pub)
	if err != nil || len(pub) != 32 {
		writeErr(out, fmt.Errorf("pub: need 32B: %w", err))
		return
	}
	msg, err := base64.StdEncoding.DecodeString(req.Message)
	if err != nil {
		writeErr(out, err)
		return
	}
	sig, err := base64.StdEncoding.DecodeString(req.Signature)
	if err != nil || len(sig) != 64 {
		writeErr(out, fmt.Errorf("signature: need 64B: %w", err))
		return
	}
	var pubArr [32]byte
	copy(pubArr[:], pub)
	var sigArr [64]byte
	copy(sigArr[:], sig)
	valid := ecc.VerifySignature(ecc.NewDjbECPublicKey(pubArr), msg, sigArr)
	writeOK(out, map[string]any{"valid": valid})
}

// noise_trace: executes a sequence of NoiseHandshake operations and returns
// all intermediate outputs. Inputs & outputs are base64. State is not peeked
// directly — if both sides produce the same ciphertexts/plaintexts/keys for
// the same script, their internal symmetric states must be identical.
//
// Input:
//
//	{
//	  "pattern": "Noise_XX_...",
//	  "header":  "b64 prologue bytes",
//	  "steps":   [ {op: "authenticate"|"mix_shared_secret"|"encrypt"|"decrypt"|"finish", ...} ]
//	}
//
// Step shapes:
//   - authenticate:       {data: "b64"}
//   - mix_shared_secret:  {priv: "b64 (32)", pub: "b64 (32)"}
//   - encrypt:            {plaintext: "b64"}   → returns {ciphertext: "b64"}
//   - decrypt:            {ciphertext: "b64"}  → returns {plaintext: "b64"}
//   - finish:             {}                    → returns {write_key: "b64", read_key: "b64"}
func handleNoiseTrace(out *bufio.Writer, arg json.RawMessage) {
	var req struct {
		Pattern string            `json:"pattern"`
		Header  string            `json:"header"`
		Steps   []json.RawMessage `json:"steps"`
	}
	if err := json.Unmarshal(arg, &req); err != nil {
		writeErr(out, err)
		return
	}
	header, err := base64.StdEncoding.DecodeString(req.Header)
	if err != nil {
		writeErr(out, fmt.Errorf("header: %w", err))
		return
	}

	nh := socket.NewNoiseHandshake()
	nh.Start(req.Pattern, header)

	results := make([]map[string]any, 0, len(req.Steps))
	for i, raw := range req.Steps {
		var s struct {
			Op         string `json:"op"`
			Data       string `json:"data,omitempty"`
			Priv       string `json:"priv,omitempty"`
			Pub        string `json:"pub,omitempty"`
			Plaintext  string `json:"plaintext,omitempty"`
			Ciphertext string `json:"ciphertext,omitempty"`
		}
		if err := json.Unmarshal(raw, &s); err != nil {
			writeErr(out, fmt.Errorf("step %d: %w", i, err))
			return
		}
		r := map[string]any{"op": s.Op}
		switch s.Op {
		case "authenticate":
			b, err := base64.StdEncoding.DecodeString(s.Data)
			if err != nil {
				writeErr(out, fmt.Errorf("step %d data: %w", i, err))
				return
			}
			nh.Authenticate(b)
		case "mix_shared_secret":
			priv, err := base64.StdEncoding.DecodeString(s.Priv)
			if err != nil || len(priv) != 32 {
				writeErr(out, fmt.Errorf("step %d priv: need 32B: %w", i, err))
				return
			}
			pub, err := base64.StdEncoding.DecodeString(s.Pub)
			if err != nil || len(pub) != 32 {
				writeErr(out, fmt.Errorf("step %d pub: need 32B: %w", i, err))
				return
			}
			if err := nh.MixSharedSecretIntoKey([32]byte(priv), [32]byte(pub)); err != nil {
				writeErr(out, fmt.Errorf("step %d mix: %w", i, err))
				return
			}
		case "encrypt":
			pt, err := base64.StdEncoding.DecodeString(s.Plaintext)
			if err != nil {
				writeErr(out, fmt.Errorf("step %d plaintext: %w", i, err))
				return
			}
			ct := nh.Encrypt(pt)
			r["ciphertext"] = base64.StdEncoding.EncodeToString(ct)
		case "decrypt":
			ct, err := base64.StdEncoding.DecodeString(s.Ciphertext)
			if err != nil {
				writeErr(out, fmt.Errorf("step %d ciphertext: %w", i, err))
				return
			}
			pt, err := nh.Decrypt(ct)
			if err != nil {
				writeErr(out, fmt.Errorf("step %d decrypt: %w", i, err))
				return
			}
			r["plaintext"] = base64.StdEncoding.EncodeToString(pt)
		case "finish":
			// We can't call Finish() because it needs a FrameSocket. Instead we
			// inline its HKDF expansion: HKDF-SHA256(salt, nil, nil) → (write, read).
			// But the salt is private; so we approximate by encrypting two marker
			// blocks to surface the current key identity, and let Python verify.
			r["error"] = "finish not exposed — compare encrypt outputs instead"
		default:
			writeErr(out, fmt.Errorf("step %d: unknown op %q", i, s.Op))
			return
		}
		results = append(results, r)
	}

	writeOK(out, map[string]any{"results": results})
}

// derive_media_keys: { media_key:"b64", app_info:"<string>" } → iv/cipher/mac/ref (all b64)
// Mirrors whatsmeow.getMediaKeys: HKDF-SHA256 over mediaKey with info=appInfo, 112 bytes.
func handleDeriveMediaKeys(out *bufio.Writer, arg json.RawMessage) {
	var wrap struct {
		MediaKey string `json:"media_key"`
		AppInfo  string `json:"app_info"`
	}
	if err := json.Unmarshal(arg, &wrap); err != nil {
		writeErr(out, err)
		return
	}
	mk, err := base64.StdEncoding.DecodeString(wrap.MediaKey)
	if err != nil {
		writeErr(out, fmt.Errorf("media_key: %w", err))
		return
	}
	expanded := hkdfutil.SHA256(mk, nil, []byte(wrap.AppInfo), 112)
	b64 := base64.StdEncoding.EncodeToString
	writeOK(out, map[string]any{
		"iv":         b64(expanded[:16]),
		"cipher_key": b64(expanded[16:48]),
		"mac_key":    b64(expanded[48:80]),
		"ref_key":    b64(expanded[80:]),
	})
}
