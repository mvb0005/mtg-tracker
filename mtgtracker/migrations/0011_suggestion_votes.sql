-- A thumbs up or down on a suggestion, with an optional note saying why: the
-- user's verdict on a candidate card, read back by whoever suggested it. Up
-- leaves the suggestion open (accept still works); down also dismisses it
-- (status 'dismissed'), so a disliked card is never suggested again. Reopen
-- clears both.
ALTER TABLE deck_suggestions ADD COLUMN vote TEXT CHECK (vote IS NULL OR vote IN ('up', 'down'));
ALTER TABLE deck_suggestions ADD COLUMN vote_note TEXT;
ALTER TABLE deck_suggestions ADD COLUMN voted_at TEXT;
