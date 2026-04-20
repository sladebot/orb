// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest';
import { safeParseWsMessage } from '../../static/app.js';

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
