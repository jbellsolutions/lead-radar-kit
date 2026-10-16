from .base import AdapterContext, Collected, SourceAdapter
from .facebook import FacebookAdapter
from .freelancer import FreelancerAdapter
from .google import GoogleAdapter
from .instagram import InstagramAdapter
from .indeed import IndeedAdapter
from .linkedin import LinkedInAdapter
from .onlinejobs import OnlineJobsAdapter
from .reddit import RedditAdapter
from .threads import ThreadsAdapter
from .tiktok import TikTokAdapter
from .weworkremotely import WeWorkRemotelyAdapter
from .youtube import YouTubeAdapter

API_ADAPTERS: dict[str, type[SourceAdapter]] = {
    a.platform: a for a in (RedditAdapter, YouTubeAdapter, TikTokAdapter, InstagramAdapter,
                            ThreadsAdapter, LinkedInAdapter, FacebookAdapter, GoogleAdapter)
}

# Job boards: read directly (polite HTTP) or, for Indeed, through Data Box `job_board`. Posts are kind "job".
LISTING_ADAPTERS: dict[str, type[SourceAdapter]] = {
    a.platform: a for a in (OnlineJobsAdapter, WeWorkRemotelyAdapter, FreelancerAdapter, IndeedAdapter)
}

__all__ = ["API_ADAPTERS", "LISTING_ADAPTERS", "AdapterContext", "Collected", "SourceAdapter"]
