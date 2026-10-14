"""Collab detection, using text from real waiting rooms."""

import unittest

from streamwatch.collab import detect_collab, short_channel_name

DEVOUR_DESC = "Collab w/\n@DubsteP_ch \n@JunoUmezono \n@MillieParfait \n\n💃MASQUERADE PARTY MERCH\nhttps://x"
VALORANT_DESC = ("Neo-Porte所属 2期生Quattro外国人担当\n久我レオ(くがれお)です！\n\nw/\n@ClaudeClawmark \n@DubsteP_ch \n"
                 "@Seible_nijisanji \n@ShuYamino \n\nNayu is here in spirit :3\n・コラボ相手や箱内でも嫌がる人も")


class CollabTest(unittest.TestCase):
    def test_handles_after_marker_in_description(self):
        self.assertEqual(detect_collab("【DEVOUR】 ビビり４人で...", DEVOUR_DESC, "@kugaleo_q"),
                         (True, ["@DubsteP_ch", "@JunoUmezono", "@MillieParfait"]))
        self.assertEqual(detect_collab("【VALORANT】 w/ Claude, Dubstep", VALORANT_DESC, "@kugaleo_q")[1],
                         ["@ClaudeClawmark", "@DubsteP_ch", "@Seible_nijisanji", "@ShuYamino"])

    def test_names_from_title_when_description_has_none(self):
        self.assertEqual(detect_collab("【VALORANT】 w/ Claude, Dubstep, Seible, Shu 【久我レオ】", "", None),
                         (True, ["Claude", "Dubstep", "Seible", "Shu"]))
        self.assertEqual(detect_collab("Karaoke ft. Juno & Millie", "", None), (True, ["Juno", "Millie"]))

    def test_handles_on_marker_line(self):
        self.assertEqual(detect_collab("Minecraft", "collab with @PartnerA and @PartnerB!", None)[1],
                         ["@PartnerA", "@PartnerB"])

    def test_not_collabs(self):
        self.assertEqual(detect_collab("Camping with KRISISIS!【SONS OF THE FOREST】",
                                       "LETS GOOOOO\nDon't bring up other streamers", "@vantacrowbringer"), (False, []))
        # Chat rules that merely mention コラボ, with no partners listed
        self.assertEqual(detect_collab("【雑談】wednesday my dudes", VALORANT_DESC.split("w/")[0] + "・コラボ相手", "@x"),
                         (False, []))
        self.assertEqual(detect_collab("Q&A w/o spoilers", "", None), (False, []))  # "w/o" means without

    def test_collab_word_without_names(self):
        self.assertEqual(detect_collab("【コラボ】 surprise guest!", "", None), (True, []))

    def test_excludes_own_handle_and_duplicates(self):
        desc = "w/\n@MyOwnChannel\n@Partner\n@partner"
        self.assertEqual(detect_collab("x", desc, "@myownchannel")[1], ["@Partner"])

    def test_short_channel_name(self):
        self.assertEqual(short_channel_name("Dubstep 【NIJISANJI EN】"), "Dubstep")
        self.assertEqual(short_channel_name("Juno Umezono / 梅園ジュノ"), "Juno Umezono / 梅園ジュノ")
        self.assertEqual(short_channel_name("【公式】"), "【公式】")


if __name__ == "__main__":
    unittest.main()
