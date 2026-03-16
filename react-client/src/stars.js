import React from 'react';
import { FaStar, FaStarHalfAlt, FaRegStar } from 'react-icons/fa'; // Import star icons

const StarRating = ({ rating }) => {
  const totalStars = 5; // Total number of stars
  const stars = [];

  for (let i = 1; i <= totalStars; i++) {
    if (i <= Math.floor(rating)) {
      // Full star
      stars.push(<FaStar key={i} color="orange" />);
    } else if (i === Math.ceil(rating) && rating % 1 !== 0) {
      // Half star
      stars.push(<FaStarHalfAlt key={i} color="orange" />);
    } else {
      // Empty star
      stars.push(<FaRegStar key={i} color="gray" />);
    }
  }

  return <div style={{ display: 'flex', gap: '5px' }}>{stars}</div>;
};

export default StarRating;