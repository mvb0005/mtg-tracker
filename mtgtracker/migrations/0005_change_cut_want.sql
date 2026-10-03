-- A change can replace a card the deck lists but you don't own yet: it cuts
-- that deck_wants row instead of a lot. At most one of cut_lot_id /
-- cut_want_id is set (enforced in deckmods). No foreign key: applying the
-- change deletes the wanted row, while the applied change stays as history.
ALTER TABLE deck_changes ADD COLUMN cut_want_id INTEGER;
