// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest';
import { safeParseWsMessage, describeHttpError } from '../../static/app.js';

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
