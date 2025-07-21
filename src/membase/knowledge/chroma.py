# -*- coding: utf-8 -*-
"""
ChromaDB-based implementation of KnowledgeBase
"""

import hashlib
import os
import json
from typing import Optional, List, Dict, Any, Union
import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
import numpy as np

from membase.storage.hub import hub_client

from .knowledge import KnowledgeBase
from .document import Document

import logging
logger = logging.getLogger(__name__)

class ChromaKnowledgeBase(KnowledgeBase):
    """ChromaDB-based implementation of KnowledgeBase."""
    
    def __init__(
        self,
        persist_directory: str = "./chroma_db",
        collection_name: str = "default",
        embedding_function: Optional[Any] = None,
        membase_account: str = "default",
        auto_upload_to_hub: bool = False,
        **kwargs: Any,
    ):
        """
        Initialize the ChromaDB-based knowledge base.
        
        Args:
            persist_directory (str): Directory to persist the database
            collection_name (str): Name of the collection to use
            embedding_function: Custom embedding function to use
            membase_account: Default account name for hub upload
            auto_upload_to_hub: Whether to automatically upload documents to hub
            **kwargs: Additional arguments for ChromaDB client
        """
        self._persist_directory = persist_directory
        self._collection_name = collection_name
        self._membase_account = membase_account
        self._auto_upload_to_hub = auto_upload_to_hub
        
        # Ensure persistence directory exists
        os.makedirs(persist_directory, exist_ok=True)
        
        # Initialize ChromaDB client
        client_settings = Settings(
            anonymized_telemetry=False,
            is_persistent=True,
            **kwargs
        )
        
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=client_settings
        )
        
        # Set up embedding function
        if embedding_function is None:
            self.embedding_function = embedding_functions.DefaultEmbeddingFunction()
        else:
            self.embedding_function = embedding_function
            
        # Get or create collection with HNSW configuration
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            embedding_function=self.embedding_function,
            metadata={
                "M": 16,  # Number of bi-directional links created for every new element
                "ef_construction": 100,  # Size of the dynamic candidate list for constructing the graph
                "ef_search": 50,  # Size of the dynamic candidate list for searching
            }
        )
    
    def add_documents(
        self,
        documents: Union[Document, List[Document]],
        strict: bool = False,
    ) -> None:
        """
        Add documents to the knowledge base.
        
        Args:
            documents: Single document or list of documents to add
            strict: Whether to strictly check for duplicate documents
        """
        if isinstance(documents, Document):
            documents = [documents]
            
        # Prepare data
        ids = []
        texts = []
        metadatas = []
        
        for doc in documents:
            # Generate unique ID if not provided
            if doc.doc_id is None:
                doc.doc_id = hashlib.sha256(doc.content.encode()).hexdigest()
            
            existing_doc = self.collection.get(ids=[doc.doc_id])
            if len(existing_doc["ids"]) > 0:
                logger.info(f"Document {doc.doc_id} already exists in the collection")
                continue

            if strict:
                is_duplicate = self.evaluate_document(doc.content)
                if is_duplicate:
                    logger.info(f"Document {doc.doc_id} is a duplicate content")
                    continue

            # Ensure metadata is a non-empty dict
            if not doc.metadata:
                doc.metadata = {"source": "default"}
            
            # add collection name to metadata
            doc.metadata["collection"] = self._collection_name
            
            ids.append(doc.doc_id)
            texts.append(doc.content)
            metadatas.append(doc.metadata)
        
        if len(ids) > 0:
            # Add to ChromaDB
            self.collection.add(
                ids=ids,
                documents=texts,
                metadatas=metadatas
            )
    
        # Upload to hub if requested
        if self._auto_upload_to_hub:
            for doc in documents:
                # doc as content in upload_hub
                # doc serialized as json string in upload_hub
                hub_client.upload_hub(
                    owner=self._membase_account,
                    filename=doc.doc_id,
                    msg=json.dumps(doc.to_dict())
                )

    def update_documents(
        self,
        documents: Union[Document, List[Document]],
    ) -> None:
        """
        Update existing documents in the knowledge base.
        
        Args:
            documents: Single document or list of documents to update
            
        Raises:
            ValueError: If document has no valid doc_id
            KeyError: If document ID does not exist in the collection
        """
        if isinstance(documents, Document):
            documents = [documents]
            
        # Prepare data
        ids = []
        texts = []
        metadatas = []
        
        for doc in documents:
            if doc.doc_id is None:
                raise ValueError("Document must have a valid doc_id for update")
            
            # Ensure metadata is a non-empty dict
            if not doc.metadata:
                doc.metadata = {"source": "default"}
            
            # add collection name to metadata
            doc.metadata["collection"] = self._collection_name
            
            ids.append(doc.doc_id)
            texts.append(doc.content)
            metadatas.append(doc.metadata)
            
            if self._auto_upload_to_hub:
                # doc as content in upload_hub
                # doc serialized as json string in upload_hub
                hub_client.upload_hub(
                    owner=self._membase_account,
                    filename=doc.doc_id,
                    msg=json.dumps(doc.to_dict())
                )
        
        try:
            # Update in ChromaDB
            self.collection.update(
                ids=ids,
                documents=texts,
                metadatas=metadatas
            )
        except ValueError as e:
            # Check which IDs don't exist
            existing_ids = set(self.collection.get()["ids"])
            non_existent_ids = [id for id in ids if id not in existing_ids]
            if non_existent_ids:
                raise KeyError(f"Documents with IDs {non_existent_ids} do not exist in the collection")
            raise e
    
    def delete_documents(
        self,
        document_ids: Union[str, List[str]],
    ) -> None:
        """
        Delete documents from the knowledge base.
        
        Args:
            document_ids: Single document ID or list of document IDs to delete
        """
        if isinstance(document_ids, str):
            document_ids = [document_ids]
            
        self.collection.delete(ids=document_ids)
    
    def exists(self, document_ids: Union[str, List[str]]) -> Union[bool, List[bool]]:
        """
        Check if document IDs exist in the collection.
        
        Args:
            document_ids: Single document ID or list of document IDs to check
            
        Returns:
            If a single ID is provided, returns a boolean indicating if it exists.
            If a list of IDs is provided, returns a list of booleans indicating which IDs exist.
        """
        if isinstance(document_ids, str):
            document_ids = [document_ids]
            
        # Get all existing IDs from the collection
        existing_ids = set(self.collection.get()["ids"])
        
        # Check existence for each ID
        if len(document_ids) == 1:
            return document_ids[0] in existing_ids
        else:
            return [id in existing_ids for id in document_ids]
    
    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.0,
        metadata_filter: Optional[Dict[str, Any]] = None,
        content_filter: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Document]:
        """
        Retrieve relevant documents for a given query.
        
        Args:
            query: The query string
            top_k: Number of documents to retrieve
            similarity_threshold: Minimum similarity score (0.0 to 1.0) for retrieved documents
            metadata_filter: Dictionary of metadata fields to filter by, using ChromaDB operators
                           e.g. {"field": {"$eq": "value"}}
            content_filter: String to filter document content
            **kwargs: Additional retrieval parameters
            
        Returns:
            List of retrieved documents
        """
        # Prepare query parameters
        query_params = {
            "query_texts": [query],
            "n_results": top_k,
            "include": ["documents", "metadatas", "distances"],
            **kwargs
        }
        if query == "":
            query_params = {
                "n_results": top_k,
                "include": ["documents", "metadatas", "distances"],
                **kwargs
            }
        
        # Add metadata filter if provided
        if metadata_filter:
            # Convert simple key-value pairs to ChromaDB operator format
            conditions = []
            for key, value in metadata_filter.items():
                if isinstance(value, dict):
                    conditions.append({key: value})
                else:
                    conditions.append({key: {"$eq": value}})
            
            # Combine conditions with $and operator
            if len(conditions) == 1:
                query_params["where"] = conditions[0]
            else:
                query_params["where"] = {"$and": conditions}
            
        # Add content filter if provided
        if content_filter:
            query_params["where_document"] = {"$contains": content_filter}
        elif similarity_threshold > 0:
            query_params["where_document"] = {"$contains": query}
        
        results = self.collection.query(**query_params)
        
        documents = []
        for i in range(len(results["ids"][0])):
            # Only include documents that meet the similarity threshold
            if similarity_threshold > 0:
                similarity = results["distances"][0][i]
                if similarity < similarity_threshold:
                    continue
                    
            doc = Document(
                content=results["documents"][0][i],
                metadata=results["metadatas"][0][i],
                doc_id=results["ids"][0][i]
            )
            doc.metadata["score"] = results["distances"][0][i]
            documents.append(doc)
            
        return documents
    
    def load(
        self,
        path: str,
        **kwargs: Any,
    ) -> None:
        """
        Load the knowledge base from a saved state.
        
        Args:
            path: Path to the saved state
            **kwargs: Additional loading parameters
        """
        # ChromaDB automatically loads from persistence directory
        pass
    
    def save(
        self,
        path: str,
        **kwargs: Any,
    ) -> None:
        """
        Save the current state of the knowledge base.
        
        Args:
            path: Path to save the state
            **kwargs: Additional saving parameters
        """
        # ChromaDB automatically saves to persistence directory
        pass
    
    def clear(self) -> None:
        """Clear all data from the knowledge base."""
        self.client.delete_collection(self._collection_name)
        self.collection = self.client.create_collection(
            name=self._collection_name,
            embedding_function=self.embedding_function
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the knowledge base.
        
        Returns:
            Dictionary containing various statistics
        """
        collection_info = self.collection.get()
        return {
            "num_documents": len(collection_info["ids"]),
            "collection_name": self._collection_name,
            "embedding_function": self.embedding_function.__class__.__name__,
            "persist_directory": self._persist_directory
        }
    
    def find_optimal_threshold(
        self,
        query: str,
        min_threshold: float = 0.3,
        max_threshold: float = 0.9,
        step: float = 0.1,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        """
        Find the optimal similarity threshold for a given query.
        
        Args:
            query: The query string
            min_threshold: Minimum threshold to try
            max_threshold: Maximum threshold to try
            step: Step size for threshold increments
            top_k: Number of documents to retrieve
            
        Returns:
            Dictionary containing threshold analysis results
        """
        results = []
        thresholds = np.arange(min_threshold, max_threshold + step, step)
        
        for threshold in thresholds:
            docs = self.retrieve(query, top_k=top_k, similarity_threshold=threshold)
            results.append({
                "threshold": threshold,
                "num_documents": len(docs),
                "documents": docs
            })
            
        # Find the threshold that gives the most balanced results
        balanced_threshold = None
        min_diff = float('inf')
        
        for i in range(len(results) - 1):
            curr_diff = abs(results[i]["num_documents"] - results[i + 1]["num_documents"])
            if curr_diff < min_diff:
                min_diff = curr_diff
                balanced_threshold = results[i]["threshold"]
        
        return {
            "balanced_threshold": balanced_threshold,
            "analysis": results,
            "recommendation": {
                "high_precision": max_threshold,
                "balanced": balanced_threshold,
                "high_recall": min_threshold
            }
        }

    def evaluate_document(
        self,
        content: str,
        top_k: int = 1
    ) -> Dict[str, Any]:
        """
        Evaluate a document based on its similarity to the query.
        
        Args:
            doc: The document to evaluate
            top_k: Number of documents to retrieve

        """
        docs = self.retrieve(content, top_k=top_k)

        # check if the document is duplicate
        for doc in docs:
            if doc.metadata["score"] < 0.2:
                return True
        return False