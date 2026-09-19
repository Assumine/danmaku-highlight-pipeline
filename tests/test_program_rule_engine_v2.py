import unittest

import program_rule_engine_v2 as engine


class StrongReactionTests(unittest.TestCase):
    def metrics(self, **changes):
        values = {
            "score": 9.5,
            "effective_messages": 8,
            "reaction_users": 3,
            "laughter_users": 2,
        }
        values.update(changes)
        return values

    def test_rejects_borderline_low_sample_reaction(self):
        self.assertFalse(engine.is_strong_reaction(self.metrics()))

    def test_accepts_enough_effective_messages(self):
        self.assertTrue(engine.is_strong_reaction(self.metrics(effective_messages=9)))

    def test_accepts_enough_reaction_users(self):
        self.assertTrue(engine.is_strong_reaction(self.metrics(reaction_users=4)))

    def test_accepts_enough_laughter_users(self):
        self.assertTrue(engine.is_strong_reaction(self.metrics(laughter_users=3)))


if __name__ == "__main__":
    unittest.main()
