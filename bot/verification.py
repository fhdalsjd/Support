from __future__ import annotations
import statistics
from dataclasses import dataclass
from .config import settings
from .telethon_client import ChannelNotAccessibleError,ChannelSnapshot,PostSnapshot,inspector,parse_post_url
@dataclass
class EligibilityResult:
    channel:ChannelSnapshot; post:PostSnapshot; average_views:float; activity_score:str; activity_notes:str; subscriber_ok:bool; average_views_ok:bool; referral_link_ok:bool
    @property
    def passed_automatic_checks(self): return self.subscriber_ok and self.average_views_ok and self.referral_link_ok
async def run_full_check(post_url:str)->EligibilityResult:
    parsed=parse_post_url(post_url); channel=await inspector.get_channel_snapshot(parsed.channel_username); post=await inspector.get_post_snapshot(parsed.channel_username,parsed.post_id)
    recent=await inspector.get_recent_view_counts(parsed.channel_username,settings.average_views_sample_size); avg=statistics.mean(recent) if recent else 0.0
    score,notes=score_activity(channel.subscriber_count,recent,avg)
    return EligibilityResult(channel,post,avg,score,notes,channel.subscriber_count>=settings.min_subscribers,avg>=settings.min_average_views,post.referral_link_found)
def score_activity(subscriber_count:int,recent_views:list[int],average_views:float)->tuple[str,str]:
    if not recent_views or subscriber_count<=0: return "SUSPICIOUS","Insufficient public data to score activity."
    notes=[]; risk=0; ratio=average_views/subscriber_count
    if ratio>.95: risk+=2; notes.append(f"Average views are {ratio:.0%} of subscriber count (unusually high).")
    elif ratio<.01: risk+=1; notes.append(f"Average views are only {ratio:.1%} of subscriber count (unusually low).")
    if len(recent_views)>=3:
        stdev=statistics.pstdev(recent_views); mean=statistics.mean(recent_views); cv=stdev/mean if mean else 0
        if cv<.05: risk+=2; notes.append("Recent post views are nearly identical (low natural variance).")
    if len(recent_views)>=4:
        s=sorted(recent_views,reverse=True); rest_mean=statistics.mean(s[1:]) if len(s)>1 else 0
        if rest_mean>0 and s[0]>rest_mean*5: risk+=1; notes.append("One post is a major outlier compared to the rest of the sample.")
    if len(recent_views)<5: risk+=1; notes.append("Very few recent posts available to sample -- treat average with caution.")
    if risk==0:return "GOOD","No anomalies detected in the sampled public data."
    if risk<=2:return "SUSPICIOUS"," ".join(notes)
    return "HIGH_RISK"," ".join(notes)
__all__=["EligibilityResult","run_full_check","score_activity","ChannelNotAccessibleError"]
