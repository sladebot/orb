// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest';
import { safeParseWsMessage, describeHttpError, buildSplitDiffRows, buildSessionCreateBody } from '../../static/app.js';

describe('describeHttpError', () => {
    it('does not echo raw response body into the user-facing message', () => {
        const body = '<html>Internal server error\nTraceback: secret-token=abc123</html>';
        const msg = describeHttpError(500, body);
        expect(msg).toContain('500');
        expect(msg).not.toContain('secret-token');
        expect(msg).not.toContain('Traceback');
        expect(msg).not.toContain('<html>');
    });

    it('includes the HTTP status code so users can see what happened', () => {
        expect(describeHttpError(502, 'bad gateway html')).toMatch(/502/);
        expect(describeHttpError(404, '')).toMatch(/404/);
    });

    it('falls back to a generic label when status is missing', () => {
        expect(describeHttpError(0, 'anything')).toMatch(/non-JSON/i);
    });
});

describe('buildSplitDiffRows', () => {
    it('emits one paired ctx row per equal op with matching line numbers', () => {
        const rows = buildSplitDiffRows([
            { type: 'equal', line: 'a' },
            { type: 'equal', line: 'b' },
        ]);
        expect(rows).toHaveLength(2);
        expect(rows[0]).toEqual({
            left: { kind: 'ctx', ln: 1, text: 'a' },
            right: { kind: 'ctx', ln: 1, text: 'a' },
        });
        expect(rows[1].left.ln).toBe(2);
        expect(rows[1].right.ln).toBe(2);
    });

    it('pairs consecutive removes and adds on the same row', () => {
        // Modifying two lines: old A,B -> new X,Y
        const rows = buildSplitDiffRows([
            { type: 'remove', line: 'A' },
            { type: 'remove', line: 'B' },
            { type: 'add', line: 'X' },
            { type: 'add', line: 'Y' },
        ]);
        expect(rows).toHaveLength(2);
        expect(rows[0].left).toMatchObject({ kind: 'del', text: 'A', ln: 1 });
        expect(rows[0].right).toMatchObject({ kind: 'add', text: 'X', ln: 1 });
        expect(rows[1].left).toMatchObject({ kind: 'del', text: 'B', ln: 2 });
        expect(rows[1].right).toMatchObject({ kind: 'add', text: 'Y', ln: 2 });
    });

    it('fills the opposite side with empty when removes outnumber adds', () => {
        // Two deletes, one add → second row has an empty right.
        const rows = buildSplitDiffRows([
            { type: 'remove', line: 'A' },
            { type: 'remove', line: 'B' },
            { type: 'add', line: 'X' },
        ]);
        expect(rows).toHaveLength(2);
        expect(rows[0].right.kind).toBe('add');
        expect(rows[1].right).toEqual({ kind: 'empty' });
    });

    it('fills the opposite side with empty when adds outnumber removes', () => {
        const rows = buildSplitDiffRows([
            { type: 'remove', line: 'A' },
            { type: 'add', line: 'X' },
            { type: 'add', line: 'Y' },
        ]);
        expect(rows).toHaveLength(2);
        expect(rows[0].left.kind).toBe('del');
        expect(rows[1].left).toEqual({ kind: 'empty' });
        expect(rows[1].right.text).toBe('Y');
    });

    it('flushes pending edits when an equal op arrives between them', () => {
        // remove, equal, add → must not pair the remove with the add across the ctx line.
        const rows = buildSplitDiffRows([
            { type: 'remove', line: 'A' },
            { type: 'equal', line: 'ctx' },
            { type: 'add', line: 'X' },
        ]);
        expect(rows).toHaveLength(3);
        expect(rows[0].left.kind).toBe('del');
        expect(rows[0].right.kind).toBe('empty');
        expect(rows[1].left.kind).toBe('ctx');
        expect(rows[1].right.kind).toBe('ctx');
        expect(rows[2].left.kind).toBe('empty');
        expect(rows[2].right.kind).toBe('add');
    });

    it('returns [] for empty or missing ops', () => {
        expect(buildSplitDiffRows([])).toEqual([]);
        expect(buildSplitDiffRows(undefined)).toEqual([]);
    });
});

describe('buildSessionCreateBody', () => {
    it('pins the selected global model for explicit solo sessions', () => {
        expect(buildSessionCreateBody({
            workdir: '/tmp/project',
            sessionConfig: { topology: 'solo', agentModels: {} },
            selectedModel: 'claude-3-5-sonnet-latest',
        })).toEqual({
            workdir: '/tmp/project',
            topology: 'solo',
            model: 'claude-3-5-sonnet-latest',
            agent_models: { solo: 'claude-3-5-sonnet-latest' },
        });
    });

    it('keeps per-agent picks and model pin together for explicit multi-agent sessions', () => {
        expect(buildSessionCreateBody({
            sessionConfig: {
                topology: 'triad',
                agentModels: { coder: 'claude-3-opus-latest' },
            },
            selectedModel: 'claude-3-5-sonnet-latest',
        })).toEqual({
            topology: 'triad',
            model: 'claude-3-5-sonnet-latest',
            agent_models: { coder: 'claude-3-opus-latest' },
        });
    });

    it('does not send a model pin when topology selection is auto', () => {
        expect(buildSessionCreateBody({
            sessionConfig: { topology: 'auto', agentModels: { solo: 'ignored' } },
            selectedModel: 'claude-3-5-sonnet-latest',
        })).toEqual({});
    });
});

describe('safeParseWsMessage', () => {
    it('returns the parsed object for valid JSON', () => {
        expect(safeParseWsMessage('{"type":"init","session_id":"abc"}'))
            .toEqual({ type: 'init', session_id: 'abc' });
    });

    it('returns null for malformed JSON (does not throw)', () => {
        expect(safeParseWsMessage('{not json')).toBeNull();
        expect(safeParseWsMessage('')).toBeNull();
        expect(safeParseWsMessage(undefined)).toBeNull();
    });

    it('returns null for non-string input', () => {
        expect(safeParseWsMessage(null)).toBeNull();
        expect(safeParseWsMessage(42)).toBeNull();
    });
});
