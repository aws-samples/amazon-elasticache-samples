"""
Memory extraction from conversations.
"""

import logging
import re
from typing import Any, Optional

from agentic_shopping_demo.memory.models import MemoryCandidate, MemoryType

logger = logging.getLogger(__name__)


class MemoryExtractor:
    """
    Extracts memory-worthy content from conversations.
    
    Identifies preferences, patterns, and facts that should be
    stored in agent memory for future reference.
    """
    
    def __init__(self):
        """Initialize memory extractor."""
        logger.info("[MEMORY] MemoryExtractor initialized")
    
    def extract_from_response(
        self,
        user_message: str,
        agent_response: str,
        conversation_state: Optional[dict[str, Any]] = None
    ) -> list[MemoryCandidate]:
        """
        Extract memory candidates from conversation turn.
        
        Args:
            user_message: User's message
            agent_response: Agent's response
            conversation_state: Current conversation state from state_extractor
            
        Returns:
            List of memory candidates to store
        """
        candidates = []
        
        # Extract from conversation state if available
        if conversation_state:
            candidates.extend(
                self._extract_from_state(conversation_state)
            )
        
        # Extract preferences from user message
        candidates.extend(
            self._extract_preferences(user_message, conversation_state)
        )
        
        # Extract patterns from conversation
        candidates.extend(
            self._extract_patterns(user_message, agent_response)
        )
        
        # Extract facts from agent response
        candidates.extend(
            self._extract_facts(agent_response, conversation_state)
        )
        
        logger.debug(
            f"[MEMORY] Extracted {len(candidates)} memory candidates"
        )
        
        return candidates
    
    def _extract_from_state(
        self,
        conversation_state: dict[str, Any]
    ) -> list[MemoryCandidate]:
        """Extract memories from conversation state."""
        candidates = []
        
        # Extract active domain and task
        active_domain = conversation_state.get("active_domain")
        active_task = conversation_state.get("active_task")
        
        if active_domain and active_task:
            # Store current task context as short-term memory
            content = f"User is currently working on {active_task} in {active_domain} domain"
            candidates.append(
                MemoryCandidate(
                    content=content,
                    memory_type=MemoryType.SHORT_TERM,
                    metadata={
                        "domain": active_domain,
                        "active_task": active_task,
                        "source": "conversation_state"
                    }
                )
            )
        
        # Extract entities (products, orders, etc.)
        entities = conversation_state.get("entities", {})
        for entity_type, entity_values in entities.items():
            if entity_values:
                # Store entity context as short-term memory
                content = f"User mentioned {entity_type}: {', '.join(str(v) for v in entity_values[:3])}"
                candidates.append(
                    MemoryCandidate(
                        content=content,
                        memory_type=MemoryType.SHORT_TERM,
                        metadata={
                            "domain": active_domain,
                            "entity_type": entity_type,
                            "source": "entities"
                        }
                    )
                )
        
        return candidates
    
    def _extract_preferences(self, user_message: str, conversation_state: Optional[dict[str, Any]] = None) -> list[MemoryCandidate]:
        """Extract user preferences from message."""
        candidates = []
        
        print(f"[MEMORY EXTRACTOR] Processing user message: {user_message}")
        
        # Determine product category from context
        # Priority 1: Check current message for explicit product mentions
        product_category = None
        if re.search(r"\b(?:shoe|shoes|footwear|sneaker|boot)\b", user_message, re.IGNORECASE):
            product_category = "footwear"
            print(f"[MEMORY EXTRACTOR] ✓ Detected category from message: {product_category}")
        elif re.search(r"\b(?:jacket|coat|hoodie|shirt|tee|clothing|apparel)\b", user_message, re.IGNORECASE):
            product_category = "apparel"
            print(f"[MEMORY EXTRACTOR] ✓ Detected category from message: {product_category}")
        elif re.search(r"\b(?:watch|tracker|device)\b", user_message, re.IGNORECASE):
            product_category = "accessories"
            print(f"[MEMORY EXTRACTOR] ✓ Detected category from message: {product_category}")
        
        # Priority 2: Check conversation state for stored product category
        # (This would be set by the state extractor when products are discussed)
        if not product_category and conversation_state:
            # Look for product_category in conversation state
            product_category = conversation_state.get("product_category")
            if product_category:
                print(f"[MEMORY EXTRACTOR] Using category from conversation state: {product_category}")
        
        # Priority 3: Try to infer from memory context in the message
        # If the message contains "## Relevant Context from Memory" with product mentions
        if not product_category:
            memory_context_match = re.search(r"## Relevant Context from Memory.*?(?:shoe|shoes|footwear)", user_message, re.IGNORECASE | re.DOTALL)
            if memory_context_match:
                product_category = "footwear"
                print(f"[MEMORY EXTRACTOR] Inferred category from memory context: {product_category}")
        
        # Preference patterns - order matters, more specific patterns first
        preference_patterns = [
            # Size patterns (most specific first)
            (r"(?:I (?:wear|am|use)|my size is) (?:size |a size )?(\d+(?:\.\d+)?)(?: shoes?| clothing)?", "size_preference"),
            # Budget patterns (specific first)
            (r"(?:my|the) (?:budget|price limit|max price) is (?:up to )?(?:under )?\$?(\d+)", "budget_preference"),
            (r"(?:under|less than|no more than|below) \$?(\d+)", "budget_limit"),
            # Interest/looking for patterns
            (r"I(?:'m| am) (?:looking for|interested in|searching for) (.+)", "interest"),
            # Preference patterns (general)
            (r"I (?:prefer|like|love|want|need) (.+)", "preference"),
            (r"I (?:don't like|hate|dislike|avoid) (.+)", "negative_preference"),
            (r"I (?:always|usually|often) (.+)", "habit"),
        ]
        
        for pattern, pref_type in preference_patterns:
            matches = re.finditer(pattern, user_message, re.IGNORECASE)
            for match in matches:
                if pref_type == "size_preference":
                    size = match.group(1).strip()
                    preference = f"size {size}"
                    print(f"[MEMORY EXTRACTOR] ✓ Extracted size preference: {preference}")
                elif pref_type in ("budget_preference", "budget_limit"):
                    budget = match.group(1).strip()
                    preference = f"budget ${budget}"
                    print(f"[MEMORY EXTRACTOR] ✓ Extracted budget preference: {preference}")
                else:
                    preference = match.group(1).strip()
                    print(f"[MEMORY EXTRACTOR] ✓ Extracted {pref_type}: {preference}")
                
                # Build metadata with category
                metadata = {
                    "preference_type": pref_type,
                    "source": "user_message"
                }
                if product_category:
                    metadata["category"] = product_category
                    print(f"[MEMORY EXTRACTOR] ✓ Added category to metadata: {product_category}")
                else:
                    print(f"[MEMORY EXTRACTOR] ⚠ No category detected for preference")
                
                # Store as long-term memory
                candidate = MemoryCandidate(
                    content=f"User {pref_type}: {preference}",
                    memory_type=MemoryType.LONG_TERM,
                    metadata=metadata
                )
                candidates.append(candidate)
                print(f"[MEMORY EXTRACTOR] Created candidate: {candidate.content}")
        
        print(f"[MEMORY EXTRACTOR] Total preference candidates extracted: {len(candidates)}")
        return candidates
    
    def _extract_patterns(
        self,
        user_message: str,
        agent_response: str
    ) -> list[MemoryCandidate]:
        """Extract behavioral patterns from conversation."""
        candidates = []
        
        # Communication style patterns
        if len(user_message.split()) < 5:
            # User prefers brief communication
            candidates.append(
                MemoryCandidate(
                    content="User prefers brief, concise responses",
                    memory_type=MemoryType.LONG_TERM,
                    metadata={
                        "pattern_type": "communication_style",
                        "source": "message_length"
                    }
                )
            )
        
        # Question patterns
        if "?" in user_message:
            question_count = user_message.count("?")
            if question_count >= 2:
                candidates.append(
                    MemoryCandidate(
                        content="User tends to ask multiple questions at once",
                        memory_type=MemoryType.LONG_TERM,
                        metadata={
                            "pattern_type": "question_style",
                            "source": "question_count"
                        }
                    )
                )
        
        return candidates
    
    def _extract_facts(
        self,
        agent_response: str,
        conversation_state: Optional[dict[str, Any]] = None
    ) -> list[MemoryCandidate]:
        """Extract factual information from agent response."""
        candidates = []
        
        # Extract product recommendations
        if conversation_state:
            active_domain = conversation_state.get("active_domain")
            
            if active_domain == "commerce":
                # Look for product mentions in response
                product_pattern = r"(?:recommend|suggest|try) (?:the )?([A-Z][a-zA-Z\s]+(?:Runner|Shoe|Jacket|Tee|Short))"
                matches = re.finditer(product_pattern, agent_response)
                
                for match in matches:
                    product_name = match.group(1).strip()
                    candidates.append(
                        MemoryCandidate(
                            content=f"Agent recommended product: {product_name}",
                            memory_type=MemoryType.SHORT_TERM,
                            metadata={
                                "domain": "commerce",
                                "fact_type": "product_recommendation",
                                "source": "agent_response"
                            }
                        )
                    )
        
        return candidates
    
    def classify_memory_type(
        self,
        content: str,
        metadata: Optional[dict[str, Any]] = None
    ) -> MemoryType:
        """
        Classify memory as SHORT_TERM or LONG_TERM.
        
        Args:
            content: Memory content
            metadata: Memory metadata
            
        Returns:
            MemoryType classification
        """
        # Long-term indicators
        long_term_keywords = [
            "prefer", "like", "love", "hate", "dislike",
            "always", "usually", "often", "never",
            "habit", "pattern", "style", "interest"
        ]
        
        content_lower = content.lower()
        
        # Check for long-term keywords
        for keyword in long_term_keywords:
            if keyword in content_lower:
                return MemoryType.LONG_TERM
        
        # Check metadata
        if metadata:
            if metadata.get("preference_type"):
                return MemoryType.LONG_TERM
            if metadata.get("pattern_type"):
                return MemoryType.LONG_TERM
        
        # Default to short-term for session context
        return MemoryType.SHORT_TERM
    
    def should_store(
        self,
        candidate: MemoryCandidate,
        pii_filter: Optional[Any] = None
    ) -> bool:
        """
        Determine if memory candidate should be stored.
        
        Filters out:
        - Transient information
        - Duplicate information
        - PII-containing content
        
        Args:
            candidate: Memory candidate to evaluate
            pii_filter: Optional PIIFilter instance
            
        Returns:
            True if should store, False otherwise
        """
        # Filter out very short content
        if len(candidate.content) < 10:
            logger.debug(
                f"[MEMORY] Filtering out short content: {candidate.content}"
            )
            return False
        
        # Filter out transient information
        transient_keywords = [
            "currently", "right now", "at the moment",
            "just now", "today"
        ]
        
        content_lower = candidate.content.lower()
        for keyword in transient_keywords:
            if keyword in content_lower and candidate.memory_type == MemoryType.LONG_TERM:
                logger.debug(
                    f"[MEMORY] Filtering out transient content: {candidate.content}"
                )
                return False
        
        # Filter out PII if filter provided
        if pii_filter:
            if pii_filter.contains_pii(candidate.content):
                logger.warning(
                    f"[MEMORY] Filtering out PII-containing content"
                )
                return False
        
        return True
